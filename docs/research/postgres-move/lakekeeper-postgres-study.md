# Lakekeeper's Postgres layer, read from source (2026-11-26)

A second, deeper study of how Lakekeeper keeps its state in one Postgres: schema, transactions,
migrations, workers, credentials and outage behaviour. It does not repeat
`design/lakekeeper.md` (request ordering, the task queue, soft delete, authorization and events at a
conceptual level). It records findings and the authors' stated reasons only.

## Sources

| Repository | Commit read | Commit date |
|---|---|---|
| `lakekeeper/lakekeeper` (`main`, shallow clone) | `2ee5516c00f8dc580203e72cdcf80e29074e60a8` | 2026-11-26T10:38:08Z |
| `lakekeeper/lakekeeper-charts` (`main`, shallow clone) | `732badae09566f2d1815a49ab8d7342d0dd1515b` | 2026-11-18T17:01:18+01:00 |

Paths are relative to `lakekeeper/` unless they start with `charts/`, which is `lakekeeper-charts/`.
`PG/` stands for `crates/lakekeeper-storage-postgres/`, `MIG/` for `PG/migrations/`, `LK/` for
`crates/lakekeeper/src/`. A claim marked **(docs)** was read only in `docs/docs/*.md`; **(inferred)** is
my reading of code I did not see run. No Postgres server was available on this host, so the final DDL
below is assembled by reading the 79 migration files in order, not from a `pg_dump`.

---

## 1. Schema design

### Conventions every table follows

- Extensions: `uuid-ossp` (for `uuid_generate_v1mc()`), `pgcrypto` (`MIG/01_setup.sql:5-6`), `pg_trgm`,
  `btree_gin` (`MIG/20241009122911_users_and_roles.sql:1-2`), `btree_gist`
  (`MIG/20250923164029_fuzzy_tabular_search.sql:1`). Postgres >= 15 is required because of
  `NULLS NOT DISTINCT` (`docs/docs/gotchas.md:53`, docs).
- Time columns come from a procedure, `add_time_columns(tablename)`: `created_at timestamptz not null
  default now()`, `updated_at timestamptz` (`MIG/20240620151429_timecolumns.sql:1-13`), and an
  `updated_at` trigger `WHEN (OLD is distinct from NEW)` (`MIG/01_setup.sql:16-30`). The stated reason:
  "can help immensely with debugging and auditing" (`MIG/01_setup.sql:7-8`).
- `project`, `warehouse`, `namespace` and `role` carry `version BIGINT NOT NULL DEFAULT 0`, bumped by one
  trigger that sets `NEW.updated_at = NOW(); NEW.version = OLD.version + 1`
  (`MIG/20251027163724_introduce_entity_versions.sql:1-37`; `role` in
  `MIG/20260220011106_string_role_id.sql:4-5,14-15`). The version gates cache re-inserts: "the cache
  writers *replace* entries (version-gated re-inserts)" (`LK/service/cache_ttl.rs:75-77`).
- The namespace trigger compares named columns instead of the whole row, because `pg_dump` emits
  `(old.* IS DISTINCT FROM new.*)`, which `pg_restore` rejects on a table with a generated column
  (`MIG/20260525120000_fix_namespace_trigger_wholerow_when.sql:1-14`).
- Names unique within a parent use a nondeterministic ICU collation:
  ```sql
  create collation case_insensitive (provider = icu, locale = 'und-u-ks-level2', deterministic = false);
  ```
  (`MIG/01_setup.sql:33-37`). "By applying our custom collation we can simply mark this column as
  `unique` and Postgres will enforce case-insensitive uniqueness … this collation doesn't support the
  `LIKE`/`ILIKE` operators" (`MIG/02_warehouse.sql:6-12`).
- Postgres enums are used for closed vocabularies (`tabular_type`, `warehouse_status`, `deletion_kind`,
  `task_intermediate_status`, `task_final_status`, `entity_type`, `grant_principal_type`,
  `grant_resource_type`, `tag_value_kind`, `managed_by`, …). The route-name enum `api_endpoints` was
  converted to `text` for endpoint statistics and idempotency records in one migration
  (`MIG/20261015120000_endpoint_names_as_text_and_task_schedules.sql:11-38,172-191`); the code gives
  the reason: "`operation` is compared as text: after a rolling downgrade this replica can read a name
  written by a newer one" (`PG/src/idempotency.rs:72-74`). `ALTER TYPE … ADD VALUE` is only safe if
  the value is not used in the same transaction: "Added here, not used in this migration, so the in-txn
  ADD VALUE is safe" (`MIG/20260720000000_tags.sql:93-95`); where a new
  value had to be used at once the enum was recreated: "We recreate the enum to use new values in the
  same transaction" (`MIG/20251228101923_enable_tasks_on_project_level.sql:8-11`).

### Ids: who mints, which version

- Catalog entities get **UUIDv7 minted by the application**: `new_random() -> Self(uuid::Uuid::now_v7())`
  for every id type built by the `define_id_type!` macro (`LK/service/identifier/generic.rs:61`), the
  project id is `Uuid::now_v7().to_string()` stored as `text` (`LK/service/identifier/project.rs:37-38`;
  project ids became text in `MIG/20250304100842_string_project_ids.sql:10-38`); e.g.
  `NamespaceId::new_random()` (`LK/server/namespace.rs:324`), task ids `Uuid::now_v7()`
  (`PG/src/tasks/task_schedule.rs:549`). `tabular.tabular_id` has no default
  (`MIG/20240620151514_tabular_identifiers.sql:6`).
- Surrogate keys of rows nobody addresses from outside default to `uuid_generate_v1mc()` in the
  database: `warehouse_id` (`MIG/02_warehouse.sql:4`, though the API passes its own), `secret_id`
  (`MIG/03_secret.sql:3`; the insert omits it, `PG/src/secrets.rs:129-130`), `grant_id`
  (`MIG/20260811000000_grants.sql:18-22`), `tag_definition_id`, `tag_id` (`MIG/20260720000000_tags.sql:18-19,48-49`).
  Reason given for v1mc: "generates UUIDs that cluster better than `gen_random_uuid()` while still being
  difficult to predict and enumerate" (`MIG/01_setup.sql:1-4`).
- A **derived id** where the row must keep its identity across delete and re-create: a task schedule's
  id is `uuid_generate_v5(<namespace>, length-prefixed project|warehouse|queue|key)`, enforced by a
  CHECK so the primary key is the only arbiter of concurrent upserts:
  ```sql
  CONSTRAINT task_schedule_id_check
      CHECK (schedule_id = task_schedule_id(project_id, warehouse_id, queue_name, schedule_key))
  ```
  (`MIG/20261015120000_endpoint_names_as_text_and_task_schedules.sql:57-77,83-86,111-112`): "Derived,
  so a schedule deleted and created again keeps its id and still sees the instance it left active.
  Never change the encoding".
- `tabular` ids are reusable across warehouses: every tabular-family primary key was rebuilt to
  `(warehouse_id, <id>)` "This allows re-using IDs across different warehouses"
  (`MIG/20250904142650_reusable_table_id.sql:1-3,240-242`). Register reuses the UUID of a dropped table
  (`PG/src/tabular/mod.rs:672-673`).

### Final DDL, table by table (as the migrations leave it)

**`project`** — `project_id text PRIMARY KEY`, `project_name text not null` (not unique), time columns,
`version` (`MIG/20241006083546_projects-table.sql:3-10`, `MIG/20250304100842_string_project_ids.sql:38`,
`MIG/20251027163724_introduce_entity_versions.sql:2-3`).

**`warehouse`** — `warehouse_id uuid PK default uuid_generate_v1mc()`, `project_id text not null`
`REFERENCES project ON UPDATE CASCADE` (no ON DELETE), `warehouse_name text collate case_insensitive`,
`storage_profile jsonb not null default '{}'`, `storage_secret_id uuid` (no FK to `secret`, inferred
from a grep of every migration), `status warehouse_status ('active','inactive')`, `protected bool`,
`tabular_expiration_seconds bigint`, `tabular_delete_mode ('soft','hard')` with
`CHECK ((tabular_expiration_seconds IS NOT NULL AND tabular_delete_mode = 'soft') OR (… IS NULL AND … 'hard'))`,
`managed_by`, `allowed_format_versions smallint[]`, `default_format_version smallint` with two CHECKs,
`version`; `UNIQUE (project_id, warehouse_name)`; unique index `(warehouse_id, project_id)` as an FK
target (`MIG/02_warehouse.sql:1-28`, `MIG/20240909120857_configurable_drops.sql:1-11`,
`MIG/20250304100842_string_project_ids.sql:41-43`, `MIG/20250409201248_cascade_delete.sql:11-12`,
`MIG/20260529120000_warehouse_format_version_policy.sql:1-12`, `MIG/20260605000000_warehouse_managed_by.sql`,
`MIG/20261015120000_…:80-81`). A warehouse is hard-deleted (`PG/src/warehouse.rs:623`); there is no soft
delete for warehouses or namespaces.

**`namespace`** — `namespace_id uuid PK`, `warehouse_id uuid not null REFERENCES warehouse` (**no ON
DELETE**: a non-empty warehouse cannot be deleted, see below), `namespace_name text[] collate
case_insensitive not null`, `namespace_properties jsonb not null`, `protected bool`, `version`,
`depth int GENERATED ALWAYS AS (array_length(namespace_name, 1)) STORED`;
`UNIQUE (warehouse_id, namespace_name)` (`unique_namespace_per_warehouse`),
`UNIQUE (warehouse_id, namespace_id)` and `UNIQUE (warehouse_id, namespace_id, namespace_name)` as FK
targets; index `(warehouse_id, depth, created_at, namespace_id)` for paging
(`MIG/04_namespace.sql:1-13`, `MIG/20250904142650_reusable_table_id.sql:338-340`,
`MIG/20250923164029_fuzzy_tabular_search.sql:28-29`, `MIG/20260219081950_improve_list_performance.sql:17-23`).
The hierarchy is the array itself: a parent is `namespace_name[1:depth-1]`, and "Descendants of P are
contiguous in the `unique_namespace_per_warehouse` btree immediately after P"
(`PG/src/namespace.rs:869-870`).

**`tabular`** (tables, views, generic tables) — `tabular_id uuid`, `warehouse_id uuid not null`,
`namespace_id uuid not null`, `name text collate case_insensitive not null`, `typ tabular_type
('table','view','generic-table')`, `metadata_location text` (NULL = staged table), `fs_protocol`,
`fs_location text`, `deleted_at timestamptz`, `deletion_kind`, `protected bool`,
`tabular_namespace_name text[] collate case_insensitive not null` (a denormalised copy of the parent's
path), time columns. Keys and constraints:

```sql
ALTER TABLE tabular ADD CONSTRAINT tabular_pkey PRIMARY KEY (warehouse_id, tabular_id);
ADD constraint unique_name_per_namespace_id unique NULLS not distinct (warehouse_id, namespace_id, name, deleted_at);
ADD CONSTRAINT tabular_warehouse_id_namespace_id_fkey
    FOREIGN KEY (warehouse_id, namespace_id) REFERENCES namespace(warehouse_id, namespace_id) ON DELETE CASCADE;
ADD CONSTRAINT tabular_warehouse_id_namespace_id_namespace_name_fkey
    FOREIGN KEY (warehouse_id, namespace_id, tabular_namespace_name)
    REFERENCES namespace(warehouse_id, namespace_id, namespace_name) ON UPDATE CASCADE;
ADD CONSTRAINT tabular_fs_location_no_trailing_slash CHECK (fs_location NOT LIKE '%/') NOT VALID;
```
(`MIG/20250904142650_reusable_table_id.sql:240-242,332-334,347-351`,
`MIG/20250923164029_fuzzy_tabular_search.sql:15-35`, `MIG/20260830000000_normalize_and_index_tabular_location.sql:16-18`).
Indexes: partial `(warehouse_id, namespace_id, created_at, tabular_id) WHERE deleted_at IS NULL AND
(metadata_location IS NOT NULL OR typ = 'generic-table')` for listing
(`MIG/20260813000000_fix_list_tabulars_index.sql:16-27`); `(warehouse_id, fs_location text_pattern_ops)`
for the location check (`MIG/20260830000000_…:4-7`); a GiST trigram index on
`concat_namespace_name_tabular_name(...)` for fuzzy search (`MIG/20250923164029_…:54-60`); a unique
index `(warehouse_id, tabular_id, tabular_namespace_name)` as an FK target for grants
(`MIG/20261111120000_grant_namespace_path.sql:10-12`). The `table`, `view`, `generic_table` and all
Iceberg detail tables reference `tabular (warehouse_id, tabular_id) ON DELETE CASCADE`
(`MIG/20250904142650_reusable_table_id.sql:353-359`).

**`secret`** — `secret_id uuid PK default uuid_generate_v1mc()`, `secret bytea not null`
(`MIG/03_secret.sql:1-9`), written `pgp_sym_encrypt($1, $2, 'cipher-algo=aes256')` and read with
`pgp_sym_decrypt` under `LAKEKEEPER__PG_ENCRYPTION_KEY` (`PG/src/secrets.rs:67-68,129-130`). The default
key is a fixed string the binary warns about (`PG/src/config.rs:6-9`, `crates/lakekeeper-bin/src/serve.rs:145-148`).

**`users`** — `id text PK` (the IdP-scoped id), `name text` (nullable since 2026-06), `email text` (not
unique any more: "Identity providers in the wild legitimately yield shared/default emails for service
principals", `MIG/20260513091606_drop_unique_user_email.sql:1-7`), `user_type`, `last_updated_with`,
`deleted_at timestamptz` (soft delete), trigram GiST indexes, partial
`(created_at, id) WHERE deleted_at IS NULL` for paging (`MIG/20241009122911_users_and_roles.sql:7-18`,
`MIG/20260607000000_role_membership_endpoints_and_user_name_nullable.sql:12-27`).

**`role`** — `id uuid PK`, `project_id text REFERENCES project` (no ON DELETE), `name`, `description`,
`source_id text not null`, `provider_id text not null default 'lakekeeper'`, `version`; unique
`(project_id, lower(name))` and `UNIQUE (project_id, provider_id, source_id)`; index
`(project_id, created_at, id)` (`MIG/20241009122911_…:22-30`, `MIG/20250304100842_…:45-50`,
`MIG/20260220011106_string_role_id.sql:1-15`).

**Assignment tables** — `role_assignment (role_id uuid, user_id text, PK (user_id, role_id), both FKs
ON DELETE CASCADE)` plus reverse index; `role_assignment_sync (user_id, project_id, provider_id, synced_at)`;
`role_members_sync (role_id PK, synced_at)` (`MIG/20260301083426_user_role_mappings.sql:3-35`);
`role_membership (parent_role_id, member_role_id, PK both, CHECK (parent_role_id <> member_role_id),
both FKs ON DELETE CASCADE)` plus reverse index for the recursive CTE
(`MIG/20260601000000_role_membership.sql:1-16`).

**`grant_assignment`** — one row per `(principal, privilege, resource)`; the whole row is the identity:
```sql
constraint grant_principal_shape check (
    (principal_type = 'user' and user_id is not null and role_id is null) or
    (principal_type = 'role' and role_id is not null and user_id is null)),
constraint grant_resource_target check ( (resource_type = 'server'
        and num_nonnulls(project_id, warehouse_id, namespace_id, tabular_id, tag_definition_id) = 0) or … ),
constraint grant_unique unique nulls not distinct
    (principal_type, user_id, role_id, resource_type, privilege,
     project_id, warehouse_id, namespace_id, tabular_id, tag_definition_id)
```
Every resource FK is `ON DELETE CASCADE`, composite for warehouse-scoped levels; `grant_resource_target`
exists because "MATCH SIMPLE semantics skip the check entirely if any of their columns is null"
(`MIG/20260811000000_grants.sql:18-100`, comment at `:35-37`). "Grants carry no mutable payload, so the whole row is the
identity. That lets a write be insert-on-conflict-do-nothing and a revoke be a delete, each reporting
exactly what changed" (`MIG/20260811000000_grants.sql:93-95`). One
partial index per access path, each ending in `(created_at, grant_id)` for keyset paging, with a long
rationale about planner estimates (`MIG/20260811000000_grants.sql:100-157`). Later: a server grant may only be held by a user
(`MIG/20261005000000_server_grants_users_only.sql:1-9`), and namespace/tabular grants carry
`namespace_name text[]` copied by a `BEFORE INSERT` trigger and kept by
`FOREIGN KEY (warehouse_id, namespace_id, namespace_name) REFERENCES namespace(...) ON UPDATE CASCADE ON
DELETE CASCADE` (`MIG/20261111120000_grant_namespace_path.sql:13-58`). Users are soft-deleted, so "their
grants are removed explicitly when the user is deleted" (`MIG/20260811000000_grants.sql:51-53`).
Authoritative only when the authorizer manages no grants itself (`MIG/20260811000000_grants.sql:2`).

**`tag_definition` / `tag_allowed_value` / `tag`** — definitions are project-scoped with unique
`(project_id, lower(name))`; attachments reference the definition `ON DELETE RESTRICT`, the warehouse,
namespace and tabular `ON DELETE CASCADE`, and a column via `(warehouse_id, tabular_id, field_id)
REFERENCES tabular_field ON DELETE CASCADE` "so a column tag outlives ordinary commits and cascades away
exactly when the column truly disappears"; `UNIQUE NULLS NOT DISTINCT (warehouse_id, namespace_id,
tabular_id, field_id, tag_definition_id, source)`; "Surrogate id; children reference it, never the name
(so rename is O(1)…)" (`MIG/20260720000000_tags.sql:15-31,48-80`).

**`task`** (active rows only) — `task_id uuid PK`, `project_id text not null REFERENCES project ON DELETE
CASCADE`, `warehouse_id uuid` (FK `task_warehouse_id_fk` with no ON DELETE,
`MIG/20250428072447_add_task_warehouse_id_idx.sql:47`), `queue_name text`, `status
task_intermediate_status ('running','scheduled','should-stop')`, `scheduled_for timestamptz not null
default now()`, `picked_up_at`, `last_heartbeat_at`, `attempt int`, `parent_task_id`, `task_data jsonb not
null`, `entity_type`, `entity_id uuid`, `entity_name text[]`, `progress`, `execution_details jsonb`,
`resource_demand bigint CHECK (>= 0)`; one active task per entity and queue:
```sql
add constraint task_project_warehouse_id_entity_type_entity_id_queue_name_key unique nulls not distinct (
    project_id, warehouse_id, entity_type, entity_id, queue_name),
```
with CHECKs tying `warehouse_id`/`entity_id`/`entity_name` to `entity_type`
(`MIG/20250523101407_tasks_store_their_state.sql:3-14,97-114`,
`MIG/20251228101923_enable_tasks_on_project_level.sql:13-25,97-133`,
`MIG/20261015120000_…:138-164`). The pick index is
`(queue_name, status, scheduled_for, task_id, (COALESCE(resource_demand, 0))) INCLUDE (resource_demand)`
and `fillfactor = 70` because "Heartbeats update non-indexed columns only; free space on the page keeps
them heap-only" (`MIG/20261015120000_…:135-136,166-170`).

**`task_log`** — one row per finished attempt, `PK (task_id, attempt)`, `status task_final_status
('failed','cancelled','success')`, copies of the task's columns plus `started_at`, `duration`, `message`
(`MIG/20250523101407_…:49-68`, project scoping in `MIG/20251228101923_…:21,131-156`).

**`task_config`** — per queue and project or warehouse: `task_config_id uuid PK default
gen_random_uuid()`, `config jsonb`, `max_time_since_last_heartbeat interval`, `UNIQUE NULLS NOT DISTINCT
(project_id, warehouse_id, queue_name)` (`MIG/20250523101407_…:39-48`, `MIG/20251228101923_…:84-90`).

**`task_schedule`** — `schedule_id uuid PK` (derived, above), `project_id text REFERENCES project ON
DELETE CASCADE`, `warehouse_id`, `queue_name`, `schedule_key`, `interval_seconds bigint` with `CHECK
(BETWEEN 60 AND 3153600000)`, `anchor`, `payload jsonb`, `enabled`, `next_run_at`, `missed_runs`,
`spec_version smallint` ("A server lists, updates and materializes only rows of versions it knows"),
`FOREIGN KEY (warehouse_id, project_id) REFERENCES warehouse (warehouse_id, project_id) ON DELETE CASCADE`;
partial due index `(next_run_at) WHERE enabled` (`MIG/20261015120000_…:86-124`).

**`idempotency_record`** — section 8.

**Soft-delete and purge bookkeeping** — no separate tables any more: `tabular.deleted_at` and
`deletion_kind`; the expiry and the purge are `task` rows in the queues `tabular_expiration` /
`soft_deletion` and `tabular_purge`, the purge's `task_data` holding `tabular_location`. The former
tables `tabular_expirations` and `tabular_purges` were folded into `task.task_data` and dropped
(`MIG/20250523101407_tasks_store_their_state.sql:15-34`).

### Name uniqueness within a parent

- Case: `case_insensitive` collation on `warehouse_name`, `namespace_name text[]`, `tabular.name` and
  `tabular_namespace_name` (above). Roles and tag definitions instead use a unique index on `lower(name)`.
- Casing that is stored: the ancestor segments of a new namespace are copied from the **locked parent
  row**, only the leaf keeps the caller's spelling, "because a case-only leaf difference is the name
  itself" (`PG/src/namespace.rs:622-636`). A repair pass rewrites prefixes that disagree, byte-wise with
  `COLLATE "C"` (`PG/src/namespace.rs:739-773`, `PG/src/tabular/mod.rs:1384-1402`), pinned to run once
  per upgrade (`PG/src/migrations/mod.rs:167-176`).
- Deleted versus live: `unique NULLS not distinct (warehouse_id, namespace_id, name, deleted_at)` — two
  live rows (both `deleted_at` NULL) collide; a live row and soft-deleted rows coexist; two soft-deleted
  rows collide only on an identical `deleted_at`. "A soft-deleted namesake therefore does not block the
  rename — the same name is already freely reusable via create" (`PG/src/tabular/mod.rs:1404-1413`).
- The constraint, not a probe, decides: "Collisions are caught by the `unique_namespace_per_warehouse`
  constraint rather than by a probe, so there is no window between checking and writing"
  (`PG/src/namespace.rs:965-967`).

### Storage location: unique and non-overlapping

- "no index on `fs_location` is unique, so nothing at the schema level backstops it. Concurrent callers
  at colliding locations serialize on advisory locks, which relies on READ COMMITTED: the check must take
  its snapshot after the lock wait to see the holder's committed row" (`PG/src/tabular/mod.rs:609-619`).
- Lock keys: one `xxh3(warehouse_id || 0 || prefix)` per path prefix of the location, the bare authority
  left out, sorted and deduplicated "so two transactions holding overlapping sets acquire them in the
  same order and cannot deadlock. `xxh3` because the value has to be the same in every replica and
  across versions" (`PG/src/tabular/mod.rs:535-568`).
- The check, inside the create's transaction, before the INSERT:
  ```sql
  SELECT pg_advisory_xact_lock(key) FROM unnest($1::bigint[]) AS key;
  SELECT ( EXISTS (SELECT 1 FROM tabular ta WHERE ta.warehouse_id = $1 AND ta.fs_location = ANY($2)
                   AND ta.tabular_id != $3)
        OR EXISTS (SELECT 1 FROM tabular ta WHERE ta.warehouse_id = $1
                   AND ta.fs_location ~>=~ ($4 || '/') AND ta.fs_location ~<~ ($4 || '0')
                   AND ta.tabular_id != $3) ) as "taken!",
         EXISTS (SELECT 1 FROM task t CROSS JOIN LATERAL (SELECT rtrim(substr(t.task_data->>'tabular_location', …), '/') AS fs_location) p
                 WHERE t.warehouse_id = $1 AND t.queue_name = 'tabular_purge'
                   AND (p.fs_location = ANY($2) OR (p.fs_location ~>=~ ($4 || '/') AND p.fs_location ~<~ ($4 || '0')))) as "purging!"
  ```
  (`PG/src/tabular/mod.rs:628-716`). `$2` is the location and all its parents (ancestor or duplicate);
  the byte range `[X/, X0)` is every descendant. Reasons in the comments: two `EXISTS` instead of one
  `OR` because a generic plan "degraded to a sequential scan"; byte operators because `LIKE` "read it as
  a pattern: a location containing `\` escaped the character after it and hid a real collision"; a
  pending purge blocks the location; "A drop deletes the row and queues the purge in one transaction, so
  this single statement sees one of the two" (`:650-673`).
- Trailing slashes are trimmed on write (`fs_location_of`, `:570-584`) and forbidden by the `NOT VALID`
  CHECK, then the existing rows are trimmed and the constraint validated by a Rust data hook that first
  fails the upgrade if trimming would put two tabulars on one location
  (`PG/src/migrations/normalize_fs_location.rs:39-69`).
- Scope: runs on create and on every view commit; "A table's commit never consults the collision check"
  (`PG/src/migrations/normalize_fs_location.rs:108-112`).

### JSONB columns and why

- `warehouse.storage_profile` (`MIG/02_warehouse.sql:14`), `namespace.namespace_properties`
  (`MIG/04_namespace.sql:6`), `task.task_data` / `task_log.task_data`, `task.execution_details`,
  `task_config.config`, `task_schedule.payload` ("Chosen and interpreted by the queue that owns the
  schedule … Becomes the `task_data`", `MIG/20261015120000_…:91-97`), and in the normalised Iceberg
  schema `type_params`, `initial_default`, `write_default` (`MIG/20260625000000_normalized_schema.sql:28-46`).
  The common thread (inferred): payloads whose shape belongs to a Rust type or a queue, not to SQL.
- Table and view properties are **not** JSONB: they are key/value rows (`table_properties`,
  `view_properties`, `generic_table_properties`), aggregated with `ARRAY_AGG(key), ARRAY_AGG(value)` on
  read (`PG/src/tabular/mod.rs:1543-1560`).
- The old JSONB copy of the schema was replaced by per-field rows (`schema_field`, `tabular_field`) with
  a reference-counting GC trigger (`MIG/20260625000000_normalized_schema.sql:1-79`).

---

## 2. Rename and move

### Table rename (same namespace)

One statement, inside the caller's write transaction (`PG/src/tabular/mod.rs:1461-1580`):
```sql
WITH locked_tabular AS (
    SELECT tabular_id, name, namespace_id, typ FROM tabular
    WHERE tabular_id = $2 AND warehouse_id = $4 AND typ = $3
      AND (metadata_location IS NOT NULL OR typ = 'generic-table') AND deleted_at IS NULL
      -- The tabular must still be the one the caller resolved: same namespace, same name.
      AND namespace_id = $5 AND name = $6
    FOR UPDATE),
locked_source_namespace AS (SELECT n.namespace_id FROM namespace n JOIN locked_tabular lt ON …
    WHERE n.warehouse_id = $4 AND n.namespace_name = $7 FOR UPDATE),
warehouse_check AS (SELECT warehouse_id FROM warehouse WHERE warehouse_id = $4 AND status = 'active'),
updated AS (UPDATE tabular t SET name = $1 FROM locked_tabular lt, warehouse_check wc, locked_source_namespace lsn
    WHERE t.tabular_id = lt.tabular_id … RETURNING …)
SELECT … FROM updated u …
```
- Locks: the tabular row and the namespace row `FOR UPDATE`.
- "Locating it by id alone would let a rename that lost a race act on whatever the winner left behind"
  (`:1473-1475`).
- An occupied destination is reported by the unique index, mapped from the constraint name
  `unique_name_per_namespace_id` to `TabularAlreadyExists`; `RowNotFound` maps to `TabularNotFound`
  (`:1404-1435`). "Letting the failing statement pick its own error also means the sequential case and a
  concurrent insert of the destination name are reported identically" (`:1411-1413`).
- "Like every constraint violation, a conflict aborts the caller's transaction … anything that swallows
  this error would make the next statement fail with `25P02`" (`:1415-1417`).

### Table move across namespaces

Same shape, plus `locked_namespace` on the destination by id **and** name, `FOR UPDATE`, and the update
`SET name = $1, namespace_id = ln.namespace_id, tabular_namespace_name = ln.namespace_name`
(`PG/src/tabular/mod.rs:1582-1707`). Three rows are locked: the tabular, its source namespace, the
destination namespace. "This is what the authorizer's re-parenting rests on: it detaches that namespace,
which is only correct while it is still the real parent. A rename that lost a race must fail, not follow
the tabular into the namespace the winner moved it to" (`:1594-1598`). The data files do not move: the
location is fixed at create and contains the table's uuid (`LK/server/tabular.rs:394-443`, inferred).

### What happens when two renames race

- The second blocks on the tabular's row lock; after the first commits, the predicate `namespace_id = $5
  AND name = $6` no longer matches the new row version, the CTE returns no row, and the statement fails
  with `RowNotFound` → `TabularNotFound` (inferred from READ COMMITTED re-evaluation and the comments at
  `PG/src/tabular/mod.rs:1473-1475,1594-1598`).
- Two renames to the same free destination name: the unique index serialises them and the loser gets
  `TabularAlreadyExists` (inferred from `:1404-1413`).
- In the authorizer the two reparentings are not ordered against each other; the function's own doc
  names this window (first study, `LK/server/tabular.rs:311`).

### Outside the transaction, and in which order

`commit_rename_with_reparent` (`LK/server/tabular.rs:325-392`): detach the old parent edge in the
authorizer **before** commit (hard error), commit, then attach the new edge after commit (logged, left to
reconciliation); on a failed commit re-attach the old edge unless the commit state is unknown. The
authorizer call is made while the transaction holds the `FOR UPDATE` row locks (inferred from the code
order; `PG/src/grant.rs:196-198` names "any handler holding `FOR UPDATE` on a parent row across a network
call" as a source of lock waits).

### Namespace rename and move

`move_namespace` (`PG/src/namespace.rs:775-1052`), in the caller's transaction:
1. `SELECT … FROM namespace n INNER JOIN warehouse w ON … w.status = 'active' LEFT JOIN namespace p ON
   <parent> WHERE n.warehouse_id = $1 AND n.namespace_id = $2 FOR UPDATE OF n` — the warehouse row is not
   locked and "the LEFT JOINed parent must not be locked (Postgres rejects locking the nullable side of an
   outer join)" (`:782-822`).
2. A byte-identical destination returns the row unchanged "so that retrying a completed move succeeds"
   (`:824-842`).
3. Protection check unless `force` (`:853-855`).
4. **A namespace with descendants cannot be moved or renamed**: "Moving a namespace P that has descendants
   is not implemented yet: the UPDATE below rewrites only this row, so descendants would keep their old
   absolute paths and stop resolving" (`:865-898`). The check is a separate statement on purpose: "under
   READ COMMITTED each statement takes a fresh snapshot, so a concurrent `create_namespace` that held the
   source row's lock while we were blocked becomes visible here" (`:872-876`).
5. The destination parent is locked `FOR KEY SHARE` and its stored spelling used (`:574-596,907-940`).
6. `UPDATE namespace SET namespace_name = $3, version = version + 1, updated_at = now()` — set explicitly
   because the trigger's `WHEN` compares a case-insensitive column, "so a case-only rename compares equal
   and the trigger never fires" (`:969-1027`).
- Tables inside the renamed namespace follow by FK cascade: `tabular_namespace_name` references
  `namespace (…, namespace_name) ON UPDATE CASCADE` (`MIG/20250923164029_…:31-35`), and grants follow the
  tabular path the same way (`MIG/20261111120000_grant_namespace_path.sql:46-50`) (inferred chain).
- The authorizer edge is moved with the same detach-commit-attach order
  (`LK/api/management/v1/namespace.rs:447-520`).
- Known gap the authors state: the parent's `FOR KEY SHARE` "does *not* stop the parent being dropped
  immediately after our commit: `drop_namespace` evaluates its emptiness guard in an unlocked statement …
  That is a pre-existing gap in `drop_namespace` … closing it needs a `FOR UPDATE` pre-lock there"
  (`PG/src/namespace.rs:900-906`).

---

## 3. Transactions in the code

- **Types.** `Transaction` trait with `begin_write`, `begin_read`, `commit`, `rollback`
  (`LK/service/catalog_store.rs:434-454`). Postgres: `begin_write` = `write_pool().begin()`;
  `begin_read` = `read_pool().begin_with("BEGIN READ ONLY")` (`PG/src/lib.rs:101-125`). On top sit
  `ReadTransaction` ("It cannot write, so at the default isolation level each of its statements sees only
  committed data", `LK/service/catalog_store.rs:287-290`) and `WriteTransaction`, which collects the
  cache keys its writes change and records them in the change log in the same transaction
  (`:324-331,378-405`).
- **Pools.** Two `sqlx` pools, read and write; the read URL falls back to the write URL
  (`PG/src/lib.rs:334-346`). Reads through `State` use the read pool (e.g. `PG/src/catalog.rs:239-241`;
  46 `read_pool()` uses against 5 `write_pool()` in that file). Reads that must see the newest commit go
  to the write pool: the idempotency check (`PG/src/idempotency.rs:62-70`), the migration status
  (`crates/lakekeeper-bin/src/wait_for_db.rs:40-45`), the cache change log
  (`docs/docs/production.md:13`, docs).
- **No nested connections.** "Never acquire a nested database connection. If a transaction is active, all
  subsequent queries must use that transaction — do not check out another connection from the read or
  write pool. Nested connections cause pool exhaustion and deadlocks." "To return updated state after a
  write, read it back **in the same transaction** — a follow-up query may hit a lagging read replica"
  (`AGENTS.md:125-126`). Store functions take `&mut sqlx::Transaction` or a `StateOrTransaction` so a
  caller can thread its transaction through (`PG/src/lib.rs:265-281`).
- **Isolation.** "Stay on the default isolation level — no `REPEATABLE READ`/`SERIALIZABLE`. Read
  co-dependent data in ONE statement; guard a read-then-write with a version compare-and-set that reports
  a mismatch as retryable. Raise the level only where one statement cannot read the data together, with a
  comment saying why" (`AGENTS.md:127`). A grep for `ISOLATION`, `REPEATABLE`, `SERIALIZABLE` over the
  crates finds no use. Several designs depend on READ COMMITTED explicitly (the location check,
  `PG/src/tabular/mod.rs:615-616`; the child check, `PG/src/namespace.rs:872-876`).
- **Optimistic concurrency.**
  - Table commit: lock the committed tables' rows `ORDER BY tabular_id FOR NO KEY UPDATE` before any
    sub-table write, compare each `metadata_location` with the one the commit was built on, fail with the
    retryable `ConcurrentUpdateError`; "Id order keeps commits to several tables from crossing. `FOR NO KEY
    UPDATE` is the lock the final `tabular` update takes, so it does not block foreign-key checks … While
    it is held, that update's compare-and-set cannot fail and is a backstop"
    (`PG/src/tabular/table/commit.rs:193-248`).
  - Drop with an expected metadata location: `AND ($5::text IS NULL OR metadata_location = $5)`
    (`PG/src/tabular/mod.rs:2095`).
  - Entity `version` columns feed cache freshness (section 1).
- **Advisory locks**, three kinds:
  - transaction-scoped, keyed by hash: location prefixes (xxh3, above); grant applies
    `pg_advisory_xact_lock(hashtextextended($1, $2))` with a per-feature seed `0x6772_616E_7461_706C`
    ("`grantapl`") and keys sorted "so a diff spanning two resources" cannot deadlock on the locks
    (`PG/src/grant.rs:117-232`); migrations `pg_advisory_xact_lock(migration_lock_id(db))`
    (`PG/src/migrations/mod.rs:407-420`); change-log pruning (`PG/src/cache_change_log/retention.rs:50,113`).
  - session-scoped, for long maintenance passes (OpenFGA reconcile): `pg_try_advisory_lock`, and the
    connection is marked `close_on_drop` so the lock dies with the session — "Returning the connection to
    the pool would leave the lock held and let a re-entrant acquire on the same recycled connection
    silently succeed"; needs a pool of at least 2 (`PG/src/advisory_lock.rs:1-80`).
  - The migration lock was moved from session to transaction scope because `pg_advisory_unlock` "is
    rejected with `25P02 in_failed_sql_transaction` on a poisoned transaction" (`PG/src/migrations/mod.rs:407-413`).
- **Timeouts.**
  - Grant apply: `SET LOCAL lock_timeout = '3s'`, deliberately not reset, so it also bounds the row locks
    taken after the advisory lock; `55P03` and `40P01` map to the retriable `GrantLockTimeout`
    (`PG/src/grant.rs:122-150,193-232`).
  - Subtree grant reads: `SET LOCAL statement_timeout = '10s'`, `57014` mapped to its own error
    (`PG/src/grant.rs:1983-2000`).
  - Change-log poll: `BEGIN; SET LOCAL statement_timeout = '…ms'` in one round trip, "A
    transaction-scoped setting also holds behind a pooler in transaction mode"
    (`PG/src/cache_change_log.rs:67-80`).
  - Migrations: `statement_timeout` and `idle_in_transaction_session_timeout` of 60 min
    (`PG/src/migrations/mod.rs:397-405`); DDL lock waits `SET LOCAL lock_timeout = '5s'` "instead of
    queueing older servers' task queries behind a long-held lock, such as a running `pg_dump`"
    (`MIG/20261015120000_…:50-55`).
  - The whole request: `max_request_time` 30 s (`LK/config.rs:1674`). A request dropped while `BEGIN` is in
    flight must not leave the pooled connection in a transaction; a test asserts it
    (`PG/src/lib.rs`, test `cancelled_begin_leaves_no_open_transaction`).
  - No server-side `statement_timeout` is set for ordinary requests (grep, inferred). The production doc
    asks the operator for `idle_in_transaction_session_timeout` "for example to `10min`" because an idle
    open transaction stops vacuum of the change log (`docs/docs/production.md:15`, docs).
- **Commit runs to completion.** `WriteTransaction::commit` runs the record-keys + commit + local eviction
  on a spawned task, "so a cancelled request cannot separate a commit from the cache updates and events
  that follow it" (`LK/service/catalog_store.rs:271-286,378-405`).
- **Error mapping.** Unique violation → 409 `EntityAlreadyExists`; SQLSTATEs `2D000 25000 25001 25P01
  25P02 25P03 40000 40001 40002 40004 40P01` → 409 `TransactionFailed` "Concurrent modification failed."
  (`PG/src/dbutils.rs:46-72`); everything else → 500 `DatabaseError`.
- **"Commit state unknown".** `COMMIT_STATE_UNKNOWN_ERROR_TYPE = "CommitStateUnknownException"`
  (`LK/service/catalog_store.rs:431-432`): "An error of type … if the backend cannot tell whether the
  transaction was committed. Any other error means it was rolled back" (`:447-450`). Classification:
  ```rust
  /// A rollback is proven only by an ERROR outside SQLSTATE 40003 and the classes 08, 53, 57, 58 and XX,
  /// or by the FATAL idle-in-transaction timeout (25P03), which ends the session before `COMMIT` runs.
  ```
  any non-database error (socket closed) is unknown (`PG/src/dbutils.rs:11-32`). Tests provoke each class
  with a deferred constraint trigger, `pg_terminate_backend`, and an idle timeout (`PG/src/lib.rs:519-580`).
  What callers do with it: **skip compensation**. Table create keeps the written metadata file and the
  authorizer tuples (`LK/server/tables/create_table.rs:92-101`); commits keep new metadata files
  (`LK/server/io.rs:130-147`); warehouse create skips cleanup and logs that "its id cannot be used again
  and the secret is orphaned" (`LK/api/management/v1/warehouse/mod.rs:505-512`); renames and moves do not
  restore the old authorizer edge and log that reconciliation must repair it
  (`LK/server/tabular.rs:344-354`). The error itself is a 500 (`ErrorModel::internal`, `PG/src/lib.rs:129-134`).

---

## 4. Migrations

- **Tool.** `sqlx` migrations embedded in the binary with `sqlx::migrate!()`
  (`PG/src/migrations/mod.rs:381`), but applied by Lakekeeper's own loop, not `sqlx migrate run`:
  every migration of core and of registered extensions in **one outer transaction**, `no_tx = true` per
  file, in ascending version order (`:319-337,392-495`). "Either every migration succeeds and the
  transaction commits, or it rolls back — partial state is impossible" (`:319-321`). Tests assert the
  rollback of core when an extension migration fails (`:1710-1745`).
- **Naming and order.** "Initial setup gets numbers in their migration file names, everything else the
  default unix timestamps of `sqlx`" (`MIG/README.md:1`): `01_setup.sql` … `05_table.sql`, then
  `YYYYMMDDHHMMSS_name.sql`. Extensions interleave by version and track themselves in
  `ext_<name>_sqlx_migrations` (`PG/src/migrations/mod.rs:63-117,459-478`).
- **Data hooks** in Rust run right after their migration, inside the same transaction
  (`get_data_migrations`, `PG/src/migrations/mod.rs:816-831`): table-metadata split, schema normalisation,
  location normalisation. "The trimming lives here rather than in the accompanying `.sql` file because a
  shipped migration's body should not change, and a later migration may need to run this again"
  (`PG/src/migrations/normalize_fs_location.rs:42-46`).
- **Post-commit steps** for work that must not hold the run's locks: the FK from `table_snapshot` to
  `"table"` is added in the migration only on an empty catalog (`MIG/20261105120000_table_snapshot_table_fkey.sql:1-13`),
  otherwise after the commit in short transactions: add `NOT VALID` with 100 ms / 20 ms lock waits and
  jittered retries "below `deadlock_timeout`, so no writer is aborted", delete orphans, then `VALIDATE
  CONSTRAINT` under a lock that does not block writes (`PG/src/migrations/table_snapshot_key.rs:1-105`).
  A failure there only warns (`PG/src/migrations/mod.rs:514-523`). Post-migration hooks (namespace
  casing repair) run after the authorizer migration and are gated on whether this run applied their
  migration (`crates/lakekeeper-bin/src/main.rs:432-470`).
- **Never edited after release.** "Never edit a migration file a release tag carries (`git tag
  --contains`): it changes the sqlx checksum. Add a new numbered file, or list the version in
  `get_changed_migration_ids()`" (`AGENTS.md:129`). That list holds six versions whose checksum is
  rewritten in the tracker instead of failing (`PG/src/migrations/mod.rs:805-814`); extensions have the
  same escape (`sha_patches`, `:108-117`).
- **Written rules.** Never schema-qualify objects in a migration, because operators can place the tables
  in another schema (`docs/docs/developer-guide.md:283-316`); extensions name tables `ext_<feature>_*`,
  FK into core only with `ON DELETE CASCADE` or `SET NULL`, and create no triggers, functions or indexes
  on core objects (`PG/src/migrations/mod.rs:71-75`, `docs/docs/developer-guide.md:345-353`).
- **Rules visible in the migrations themselves** (the convention for zero-downtime DDL):
  - take every lock up front, strongest per table, in the order older servers take them, and bound the
    wait (`MIG/20261015120000_…:1-4,46-55`; `MIG/20261111120000_grant_namespace_path.sql:3-8`);
  - add CHECKs and FKs `NOT VALID` when existing rows satisfy them or are fixed later
    (`MIG/20261015120000_…:138-164`, `MIG/20260830000000_…:12-15`);
  - "A unique index, not a constraint: adding a constraint takes ACCESS EXCLUSIVE on tabular, which
    blocks table loads" (`MIG/20261111120000_grant_namespace_path.sql:10`); old FKs are kept because
    "dropping a key takes ACCESS EXCLUSIVE … which deadlocks with grant writes and blocks table loads"
    (`:65`);
  - copy-then-swap for a column type change: "Copied without blocking writes of older servers; what they
    write after the copy is lost" (`MIG/20261015120000_…:23-27`);
  - keep the unique key older servers match on (`:42-44`); a trigger fills a new column "so older servers
    keep writing grants" (`MIG/20261111120000_…:15`); "default collation for their cached plans"
    (`MIG/20261015120000_…:187`).
- **When migrations run in a deployment.** The chart renders a `Job` `…-db-migration-<revision>` running
  `migrate`, as a Helm `post-install,post-upgrade` hook with weight `-100` and
  `before-hook-creation` delete policy, and as an Argo CD `Sync` hook, wave `0`
  (`charts/lakekeeper/templates/db-migration.yaml:1-24,75-91`); with `helmWait: true` it is a plain Job
  so `helm install --wait` works (`charts/lakekeeper/values.yaml:18-21`). Its init container runs
  `wait-for-db -d -r 100 -b 2`; the catalog Deployment's init container runs `wait-for-db -dm -r 100 -b 2`,
  i.e. waits up to ~200 s for the migration to be complete (`charts/lakekeeper/templates/_pods.tpl:30-55`,
  `charts/lakekeeper/templates/catalog/catalog-deployment.yaml:86`). `catalog.dbMigrations.enabled: false`
  hands the step to the operator (`charts/lakekeeper/values.yaml:245-248`). The binary can also migrate
  before serving (`debug.migrate_before_serve`, `crates/lakekeeper-bin/src/main.rs:292-306`).
- **At start.** `serve` calls `wait_for_db(check_migrations = true, retries = 0)` unless `--force-start`
  (`crates/lakekeeper-bin/src/main.rs:472-494`). States (`PG/src/migrations/mod.rs:768-784`):
  `Complete` starts; `Missing`/`NoMigrationsTable` (schema behind) refuses; `Ahead` (a newer binary
  migrated) refuses at once, "Retrying never resolves this (the DB won't get older)"; `SchemaMismatch`
  (session resolves to another schema) refuses (`crates/lakekeeper-bin/src/wait_for_db.rs:37-101`). The
  `Ahead` test: "an older binary's migrations are a subset of what's applied, so nothing looks missing"
  (`PG/src/migrations/mod.rs:1338-1342`).
- **Rolling upgrade.** Old pods keep serving while the hook Job migrates; new pods wait in their init
  container until the schema is `Complete` (chart, above). Migrations are written to be compatible with
  the previous release still running (the rules above). A migration run that applies anything records
  "every domain" in the cache change log, "and instances of the previous release keep serving during a
  rolling upgrade" (`PG/src/migrations/mod.rs:497-505`). Read-only maintenance mode can be confirmed on
  every pod "before running database migrations" (`LK/service/health.rs`, `HealthState.maintenance_mode`
  doc; `docs/docs/configuration.md:751-764`, docs).
- **Downgrade.** None. Down migrations are skipped (`PG/src/migrations/mod.rs:466-476`; test "A down
  migration is never applied", `:1449`). An older binary refuses an `Ahead` database; `serve
  --force-start` is the documented emergency override "accepting the risk of schema incompatibility"
  (`crates/lakekeeper-bin/src/wait_for_db.rs:56-61`). Backups before every upgrade are the advice
  (`docs/docs/production.md:31`, docs).
- **Long backfills.** Inside the single migration transaction (the normalised-schema backfill "can run
  minutes on large catalogs", `PG/src/migrations/mod.rs:397-399`), except steps whose locks would block
  writers, which go after the commit (above). No batched online backfill framework was found (inferred).
- **Schema placement.** `LAKEKEEPER__PG_SCHEMA` sets `search_path` per connection with
  `set_config('search_path', $1, false)` (`PG/src/lib.rs:283-311`); `migrate` fails unless
  `current_schema()` is the configured one (`PG/src/migrations/mod.rs:278-292`); behind a transaction-mode
  pooler set it on the role instead (`docs/docs/configuration.md:107-113`, docs).

---

## 5. Connection and credentials

- **How they arrive.** Environment only, prefixes `LAKEKEEPER__` / `ICEBERG_REST__`, read once into a
  `LazyLock` (`PG/src/config.rs:11,210-229`): either `PG_DATABASE_URL_READ` / `_WRITE` or
  `PG_HOST_R`/`PG_HOST_W`, `PG_PORT`, `PG_USER`, `PG_PASSWORD`, `PG_DATABASE`, `PG_SSL_MODE`,
  `PG_SSL_ROOT_CERT` ("Connection strings take precedence", `docs/docs/configuration.md:58`; code
  `PG/src/lib.rs:334-393`). The chart sets user and password from a Secret via `secretKeyRef`
  (`externalDatabase.userSecret` / `passwordSecret`) or puts them in its own `-config-envs` Secret
  (`charts/lakekeeper/templates/_helpers.tpl:147-230`, `charts/lakekeeper/values.yaml:615-656`). No file-based
  password or `PGPASSFILE` support was found (grep).
- **TLS.** `PgSslMode` `disable|allow|prefer|require|verify-ca|verify-full`, default `prefer` when built
  from parts (`PG/src/config.rs:62-100`, `PG/src/lib.rs:366`), plus a root certificate path (`:368-372`).
- **Pools.** `PG_READ_POOL_CONNECTIONS` 10, `PG_WRITE_POOL_CONNECTIONS` 5, `PG_ACQUIRE_TIMEOUT` 5 s,
  `PG_TEST_BEFORE_ACQUIRE` false, `PG_CONNECTION_MAX_LIFETIME` unset (`PG/src/config.rs:54-58`;
  `crates/lakekeeper-bin/src/serve.rs:151-162`). Worker pods have their own pools; "Plan the connection
  limit of your database for all API pods plus the `maxReplicaCount` of every ScaledJob"
  (`docs/docs/task-workers.md:83-86`, docs). Pool gauges and an acquire-timeout counter are exported
  (`PG/src/pool_metrics.rs:12-20`).
- **Read replicas.** Supported through the read URL. Constraints (docs): the write URL "must always connect
  to the primary database, because cache invalidation reads the change records there"; replicas must lag
  less than `CACHE__INVALIDATION__REEVICT_GRACE` (10 s); PgBouncer in transaction mode works, query-routing
  poolers on the write URL do not (`docs/docs/production.md:12-14`).
- **Rotation.** No rotation handling: the password is captured at startup into the connect options of
  each pool and reused for every new connection (`PG/src/config.rs:11`, `PG/src/lib.rs:334-366`); with
  `max_lifetime` unset by default connections are not recycled (inferred). A new password therefore needs a
  restart (inferred). No IAM / short-lived database token support was found (grep for `iam`, `token`,
  `rds` in `PG/src`).
- **Least privilege.** A low-privilege role can run Lakekeeper if an administrator pre-creates the five
  extensions and the schema (`docs/docs/configuration.md:85-113`, docs; tests
  `test_migrate_into_custom_schema_as_low_privilege_user`, `PG/src/migrations/mod.rs:953-1004`). The same
  role migrates and serves: `migrate` and `serve` read the same `CONFIG` (`crates/lakekeeper-bin/src/main.rs:432-437`).

---

## 6. When Postgres is unavailable

- **`/health`.** A background loop per provider runs `update_health` every
  `health_check_frequency_seconds` (default 10, `LK/config.rs:1652`) plus 0–500 ms jitter, bounded by that
  interval (`LK/service/health.rs:14-47`). The Postgres provider runs `SELECT 1` on each pool and stores
  `read_pool` / `write_pool` as `ok`/`error` (`PG/src/lib.rs:168-209`); status starts `unknown`
  (`:186-189`). `GET /health` returns 200 only if every entry is `ok`, else 503 with the JSON
  (`LK/api/router.rs:346-372`, `LK/service/health.rs:158-185`).
- **Probes.** The chart points both liveness and readiness at `/health` on 8181, period 5 s, failure
  threshold 5 (`charts/lakekeeper/templates/catalog/catalog-deployment.yaml:113-132`,
  `charts/lakekeeper/values.yaml:175-190`). So a Postgres outage longer than roughly the health interval
  plus 25 s turns every catalog pod unready **and** restarts it (inferred).
- **Requests.** A request needing a connection waits up to the 5 s acquire timeout, then fails;
  `PoolTimedOut` is not a database error and maps to 500 `DatabaseError` (`PG/src/lib.rs:101-125`,
  `PG/src/dbutils.rs:46-71`) (inferred; I found no 503 mapping for pool errors).
- **Caches** keep serving until their TTL: warehouse 60 s, namespace 60 s, secrets 600 s, role 120 s,
  each jittered down by up to 10 % (`docs/docs/configuration.md:508-607`, docs; `LK/service/cache_ttl.rs:31`).
  "If an instance cannot read the change records for a while … it removes the changed entries when it
  can read the change records again. Until then, an entry stays at most until its time-to-live ends"
  (`docs/docs/configuration.md:519`, docs).
- **Startup.** `serve` requires the migration check to pass with no retries
  (`crates/lakekeeper-bin/src/main.rs:488-491`), and the pools are built with `connect_with`, which opens a
  connection (`PG/src/lib.rs:69-85`), so a process does not start without the database (inferred). The
  chart's init containers retry 100 × 2 s.
- **Retry and backoff.** sqlx reconnects lazily on the next acquire (inferred). The change-log poller
  backs off "the interval doubled per failure up to 10 s or the interval if longer"
  (`LK/service/cache_invalidation/poller.rs:15-16,192-206`); a first read that fails or takes over 10 s
  makes the first successful read evict everything (`:91-120`).
- **Workers.** A failed pick logs and sleeps 5 s (`LK/service/tasks/mod.rs:1358-1377`); recording a
  task's outcome is retried 5 times, 1 s apart, then logged (`:1503-1542`); a lost attempt is recovered by
  heartbeat timeout on the next pick (section 7). A pick in flight is not raced against shutdown: "a pick
  dropped mid-flight can leave a task it claimed running until its heartbeat times out" (`:1356-1357`).

---

## 7. Workers

- **Same binary, same database role.** A worker is the server with `LAKEKEEPER__SERVE_HTTP_API=false`;
  "They need the same configuration as the API pods, including `LAKEKEEPER__PG_ENCRYPTION_KEY`"
  (`docs/docs/configuration.md:26,135`, docs). No separate, narrower database role exists in the code or
  chart (inferred from `crates/lakekeeper-bin/src/serve.rs:135-162`). Every API process also runs the
  built-in workers by default (`docs/docs/configuration.md:133`, docs). Change-log pruning runs only on
  API processes "so a headless worker keeps its small pool for its tasks"
  (`LK/service/cache_invalidation/poller.rs:49-50`).
- **Enqueue in the caller's transaction.** `enqueue_task(conn: &mut PgConnection, …)` inserts with the
  unique key and, on conflict, reads back the active task holding it, retrying up to 3 times if that task
  finishes in between (`PG/src/tasks.rs:45-46,238-273`). A drop deletes or soft-deletes the row and queues
  the purge or expiry in the same transaction (`PG/src/tabular/mod.rs:670-671`).
- **Pick.** First fail at most one attempt of the queue whose heartbeat timed out
  (`fail_timed_out_attempt`, `FOR UPDATE OF t SKIP LOCKED LIMIT 1`, per-queue
  `max_time_since_last_heartbeat` from `task_config` or the default; `PG/src/tasks.rs:909-960`). Then:
  ```sql
  WITH picked_task AS (
      SELECT c.task_id, c.attempt, c.warehouse_id, c.project_id
      FROM unnest($1::text[]) AS q(queue_name)
      CROSS JOIN LATERAL (
          SELECT … FROM task t
          WHERE t.queue_name = q.queue_name AND t.scheduled_for <= now() AND t.status = 'scheduled'
            AND COALESCE(t.resource_demand, 0) > $3 AND COALESCE(t.resource_demand, 0) <= $4
          ORDER BY t.scheduled_for, t.task_id
          FOR UPDATE OF t SKIP LOCKED
          LIMIT 1) c
      ORDER BY c.scheduled_for, c.task_id LIMIT 1), …
  UPDATE task SET status = 'running', progress = 0.0, execution_details = NULL,
      picked_up_at = now(), last_heartbeat_at = now(), attempt = task.attempt + 1
  FROM picked_task p WHERE task.task_id = p.task_id AND task.attempt = p.attempt
  RETURNING …, (select config from picked_config), …
  ```
  (`PG/src/tasks.rs:614-750`). The lateral-per-name form avoids sorting the backlog; `SKIP LOCKED` "never
  hides the due rows behind it".
- **Heartbeat.** `UPDATE task SET last_heartbeat_at = now(), progress = $2, execution_details = $3 WHERE
  task_id = $1 AND attempt = $4 RETURNING status`; `should-stop` or `scheduled` tells the worker to stop,
  no row means the attempt is no longer active (`PG/src/tasks.rs:1599-1634`). The `attempt` column fences
  a stale worker (inferred from every write keying on `(task_id, attempt)`).
- **Success.** One statement: `DELETE FROM task WHERE task_id = $1 AND attempt = $3 AND status !=
  'scheduled' RETURNING *` feeding `INSERT INTO task_log … 'success'` (`PG/src/tasks.rs:1010-1060`).
- **Failure and retry.** `reschedule_failed_task` locks the row, logs the attempt to `task_log` and sets
  the next `scheduled_for`; `fail_task` moves it to the log as failed (`PG/src/tasks.rs:1192-1300`). Default
  policy: 30 s doubling to at most 1 h, shortened by up to 10 % from the task id and attempt "so that tasks
  failing together do not retry together" (`LK/service/tasks/mod.rs:222-239`), ten tries in total, about
  three hours (`docs/docs/configuration.md:137`, docs). A panicking retry hook falls back to the default
  backoff (`LK/service/tasks/mod.rs:164-176`).
- **Log cleanup.** A built-in queue `task_log_cleanup` deletes `task_log` rows past retention, queued on a
  fixed cadence (`PG/src/tasks/cleanup_task_logs_older_than.rs`; `docs/docs/configuration.md:139,150`, docs).
- **Schedules → tasks.** A materializer loop on every API server claims due schedules whose previous
  instance is not active, `FOR UPDATE OF s SKIP LOCKED`, and in one statement inserts one task per schedule
  with `entity_id = schedule_id`, `ON CONFLICT (project_id, warehouse_id, entity_type, entity_id,
  queue_name) DO NOTHING`, and advances `next_run_at` and `missed_runs` only for rows actually inserted
  (`PG/src/tasks/task_schedule.rs:501-620`). "Ticks that fall due meanwhile … share that one run and are
  counted in `missed_runs`" (`LK/service/tasks/task_schedule.rs:1-7`); the loop sleeps at most 30 s, half
  the minimum interval (`:35-39,418`). Test: concurrent materializers create one task per tick
  (`PG/src/tasks/task_schedule.rs:1085`).
- **Demand endpoint.** `GET /task-queue/<queue>/demand[?class=]` on the metrics port of API pods counts due,
  heartbeat-expired and running tasks per resource-demand class for KEDA, unauthenticated, counts only;
  it reports 0 when every API pod is read-only (`PG/src/tasks.rs:765-907`, `LK/service/tasks/queue_demand.rs:1`;
  `docs/docs/task-workers.md:7-79`, `docs/docs/monitoring.md:280-298`, docs). Workers as KEDA Jobs stop on
  `TASK_IDLE_EXIT_AFTER` / `TASK_MAX_LIFETIME`, refused on API processes (`docs/docs/configuration.md:155-171`, docs).

---

## 8. Idempotency

The first study covers the endpoint list and the replay semantics; deeper points:
- **Table.** `idempotency_record (idempotency_key uuid, warehouse_id uuid REFERENCES warehouse ON DELETE
  CASCADE, operation text, http_status int not null, created_at timestamptz default now(), PRIMARY KEY
  (warehouse_id, idempotency_key))` plus an index on `created_at` (`MIG/20260318120000_idempotency_record.sql:1-36`;
  `operation` became text in `MIG/20261015120000_…:188-190`). Scope of the key: **per warehouse**.
- **When written.** Inside the mutation's transaction, just before commit, `INSERT … ON CONFLICT
  (warehouse_id, idempotency_key) DO NOTHING RETURNING TRUE` (`PG/src/idempotency.rs:189-213`).
  "Always set: the key is inserted atomically inside the mutation transaction, so it only exists if the
  mutation committed successfully" (`MIG/20260318120000_…:8-11`).
- **Check.** On the **write pool**, because "a replica that misses a just-committed record loses the
  replay entirely — `createTable` re-runs and dies on the tabular-name unique violation as a 409"
  (`PG/src/idempotency.rs:62-88`). Another operation's key → 400 `IdempotencyKeyReused`; a non-2xx status
  is refused as `CorruptIdempotencyRecord` rather than replayed or re-run (`:151-186`).
- **Expiry and cleanup.** Lifetime `PT30M` + grace `PT5M` (`docs/docs/configuration.md:770-781`, docs).
  Cleanup is probabilistic and inline: ~1 % of checks spawn a task that deletes at most 1000 expired rows
  by `ctid`, guarded by an in-process compare-and-swap slot with a 30 s takeover
  (`PG/src/idempotency.rs:11-53,90-145`). There is no cross-replica coordination of cleanup (inferred).

---

## 9. Listing and pagination

- **Keyset on `(created_at, id)`**, token `base64url("1&<created_at micros>&<id>")`; `V2` adds a ceiling
  instant for subtree walks so every page "reads under the instant page one bound"
  (`PG/src/pagination.rs:1-100`).
- The authors note the limit of that order: "`created_at` defaults to `now()`, which is the transaction
  start, so a row committed after a page was read can carry a stamp below that page's position and be
  returned on no page at all … Any caller that concludes *completion* from exhaustion — a drain loop, a
  sweep that acts on the complement — needs a cutoff that excludes in-flight transactions instead"
  (`PG/src/pagination.rs:6-15`).
- **List tabulars**: `AND ((t.created_at > $7 OR $7 IS NULL) OR (t.created_at = $7 AND t.tabular_id > $8))
  ORDER BY t.created_at, t.tabular_id LIMIT $9`, soft-deleted rows excluded by the flags
  `include_active` / `include_deleted` / `include_staged` (`PG/src/tabular/mod.rs:906-985`), served by the
  partial index whose predicate must match the active branch exactly: "if the query can return a row the
  index does not contain, the planner silently stops using the index"
  (`MIG/20260813000000_fix_list_tabulars_index.sql:1-27`). The join to the expiry task is written so the
  planner keeps the index (`PG/src/tabular/mod.rs:951-958`).
- **Namespaces**: index `(warehouse_id, depth, created_at, namespace_id)`, the generated `depth` column
  because "array_length is not indexable" (`MIG/20260219081950_improve_list_performance.sql:17-23`).
- **Grants**: one partial keyset index per access path (section 1).
- Page size: `CONFIG.page_size_or_pagination_max` (`PG/src/tabular/mod.rs:917`).

---

## 10. Caches and invalidation

- **What `cache_change_log` is.** `(id bigint identity, xid xid8 default pg_current_xact_id(), domain
  text, keys text[], changed_at timestamptz default clock_timestamp(), PRIMARY KEY (xid, id))`, autovacuum
  tuned by fixed row counts, and a stable function `lakekeeper_record_cache_change(p_domain, p_keys)` for
  extensions (`MIG/20261021120000_cache_change_log.sql:1-44`). Every write that changes a cached entity
  records its keys in the same transaction, at most 1000 keys per row (`PG/src/cache_change_log.rs:13-30`;
  `LK/service/catalog_store.rs:324-331`). Rule: "Write cached entities through store methods that take
  `&mut WriteTransaction` … never evict by hand after a write"; "Fill shared caches only from reads that
  see committed data"; "cross-replica eviction is best effort, and the TTL stays the bound"
  (`AGENTS.md:92-94`).
- **How a replica learns.** Each instance polls the **primary** with a snapshot diff: rows whose `xid` was
  in the previous snapshot's in-progress list, or at/after its `xmax` and below the current `xmax`, and
  only those the current snapshot sees as finished (`PG/src/cache_change_log/reader.rs:26-117`). This
  returns each committed row once regardless of commit order; a test runs random writers with rolled-back
  savepoints and asserts "Every committed row is returned exactly once, and no row that was rolled back"
  (`:480-514`).
- **Bounds.** Poll every `PT1S` (100 ms–60 s) with 10 % jitter; each change is evicted twice, the second
  time after `REEVICT_GRACE` `PT10S` "for a read replica that had not yet replayed the change"
  (`LK/service/cache_invalidation/poller.rs:82-90,209-213`; `docs/docs/configuration.md:515-526`, docs).
  Rows are kept for twice the longest TTL, 2 h to 30 days, so "A poller that misses a pruned row then only
  misses entries that have expired" (`LK/service/cache_invalidation/poller.rs:215-225`); one instance at a
  time prunes, every 5 min (`:17-18,242-243`). The writing instance evicts locally at commit
  (`LK/service/catalog_store.rs:378-405`). Staleness bound stated by the docs: "the time-to-live, plus the
  delay of your read replicas, is the longest time" (`docs/docs/configuration.md:519`, docs).
- **Policy by path.** "Cold path (management/admin routes): bypass per-process in-memory caches; read
  authoritative data from the DB. Hot authz path: may tolerate cache lag" (`AGENTS.md:90-91`).

---

## 11. Tests of the database layer

- `#[lakekeeper_storage_postgres::db_test]` gives each test its own database `CREATE DATABASE … TEMPLATE
  <migrated template>`; the template is built once per server per migration change, under a session
  advisory lock (`lktmplte`), named by a hash that includes the Rust migration hooks' sources, and built
  under a temporary name then renamed "so a template that exists is always fully migrated"; a passing
  test's database is dropped, a failing one kept (`PG/src/test_utils/test_db.rs:1-200`,
  `crates/test-macros/src/lib.rs:1-11`). `#[sqlx::test]` is used only where an empty database is needed
  (`AGENTS.md:133`). Postgres comes from `DATABASE_URL`, a container started by the developer
  (`CONTRIBUTING.md:29-41`, docs).
- Race and transaction tests hold one transaction open on one connection and act on another, often
  waiting until `pg_stat_activity` shows a `Lock` wait (`crates/lakekeeper-integration-tests/src/lib.rs:538-551`):
  - a create holds location locks and a contender cannot take every lock a colliding create needs
    (`PG/src/tabular/mod.rs:2245-2310`);
  - concurrent pickers pick each task once (`PG/src/tasks.rs:2325`); concurrent materializers and first
    upserts make one row (`PG/src/tasks/task_schedule.rs:1085,1136`);
  - concurrent property commits do not deadlock; a stale commit gets `ConcurrentUpdateError`
    (`PG/src/tabular/table/commit.rs:1283,1365`);
  - a warehouse delete with a concurrently inserted task; protection changed concurrently
    (`PG/src/warehouse.rs:2053,2117`);
  - the grant advisory lock is held to commit; `55P03`/`40P01` map to the retriable error, provoked for real
    (`PG/src/grant.rs:3171,3211-3240`);
  - role delete and member sync do not deadlock (`PG/src/role_assignment.rs:2921`);
  - commit-state classification by SQLSTATE, terminated session, idle timeout, cancelled `BEGIN`
    (`PG/src/lib.rs:500-600`);
  - migrations: atomic rollback with extensions, `Ahead` detection, schema mismatch, low-privilege role,
    lock released after a poisoned run (`PG/src/migrations/mod.rs:1238-1760`).
- House rule for assertions: "Assert exact expected values. Never `assert x in (a, b)`" (`AGENTS.md:134`).

---

## 12. What the authors say is unsolved or would be done differently

- Moving or renaming a namespace with descendants "is not implemented yet" (`PG/src/namespace.rs:865-867`).
- `drop_namespace` checks emptiness in an unlocked statement and deletes by an id list from that
  snapshot: "a pre-existing gap in `drop_namespace`" (`PG/src/namespace.rs:900-906`).
- Two concurrent re-parentings are not ordered in the authorizer, a failed post-commit attach leaves the
  object parentless, and "Reconciliation is the repair path, not retry" (first study;
  `LK/server/tabular.rs:318`).
- Keyset paging on `created_at` can skip rows committed late; callers that infer completion need another
  cutoff (`PG/src/pagination.rs:6-15`).
- Cross-replica cache eviction is "best effort, and the TTL stays the bound" (`AGENTS.md:94`).
- A table commit does not run the location-overlap check; pre-existing overlaps are only reported by the
  upgrade (`PG/src/migrations/normalize_fs_location.rs:106-112,169`).
- The endpoint-statistics copy accepts losing what older servers write during the copy
  (`MIG/20261015120000_…:23`).
- Leftovers: a TODO on the old location index (`MIG/20240620151514_tabular_identifiers.sql:19-20`), "TODO:
  lift this from DB module?" (`PG/src/pagination.rs:1`), "ToDo: Get constraint name from const"
  (`PG/src/warehouse.rs:175`). A grep for `TODO|FIXME|known gap|not implemented` over `PG/` found nothing
  else.
- A warehouse whose create had an unknown commit outcome leaves an id that "cannot be used again and the
  secret is orphaned" (`LK/api/management/v1/warehouse/mod.rs:505-512`).

---

## Could not confirm

- **The final DDL from a live database.** No Postgres server on this host (only `psql`/`pg_dump` client
  binaries in `/usr/lib/postgresql/16/bin`), and docker is not allowed; the DDL above is assembled from the
  migration files. I did not read the Iceberg detail tables (`table_*`, `view_*`) or `endpoint_statistics`
  / `warehouse_statistics` in full, nor every `ALTER` on `task` (e.g. where `progress`, `entity_name`,
  `execution_details` were added); grep of `MIG/` was the method.
- **Whether `warehouse.storage_secret_id` has any FK** — a grep for `storage_secret_id` across `MIG/`
  shows only the column definition in `02_warehouse.sql`.
- **HTTP status for a pool acquire timeout** — read from `into_error_model`; not exercised.
- **Behaviour on credential rotation and DNS failover** — inferred from the config being read once and
  `max_lifetime` defaulting to none; no code or doc addresses rotation (grep `rotat` in `PG/src` found none).
- **Liveness restarts during an outage** — inferred from the chart probing `/health` for liveness; not
  observed.
- **Lakekeeper Plus** (private) may change any of this; out of reach.
- **The idempotency replay path in handlers** (what body is re-derived) — taken from the first study, not
  re-read here.
- **Task log retention default** and the cleanup queue's SQL — only the module name and docs were read
  (`PG/src/tasks/cleanup_task_logs_older_than.rs` not opened).
