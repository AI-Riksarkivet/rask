# Location exclusivity in Lakekeeper and Apache Polaris: one live table per location, and DROP against RENAME

Date: 2026-10-19. Read-only research for LH-458 (GitHub issue #571). Nothing in either clone was built or run.
Question: how do Lakekeeper and Apache Polaris keep a table's storage location held by exactly one live table, and
how does a DROP racing a RENAME of the same table stay safe?

Sources, shallow clones read as text:
- lakekeeper@5355a1b = 5355a1bbbe7c37bd9ffc16b889874a294250923d (committed 2026-10-17)
- polaris@1a8dfd7 = 1a8dfd7bcbcf27fff5d2964d98b38424c1eefdd9 (committed 2026-10-18)

Citation form `<project>@<sha7>:<path>:<line>`. Repeated directories are abbreviated: PG = `crates/lakekeeper-storage-postgres/src`,
LKS = `crates/lakekeeper/src`, PCORE = `polaris-core/src/main/java/org/apache/polaris/core`, PSVC =
`runtime/service/src/main/java/org/apache/polaris/service`, PJDBC =
`persistence/relational-jdbc/src/main/java/org/apache/polaris/persistence/relational/jdbc`, PJRES =
`persistence/relational-jdbc/src/main/resources`, PNOSQL =
`persistence/nosql/persistence/metastore/src/main/java/org/apache/polaris/persistence/nosql/metastore`. Issue and PR text
(`lakekeeper#2005`, `polaris#2295`) was read through `gh`, read-only. "(inference)" marks a reading of code that no test or
doc states. What could not be established is section 5.

---------------------------------------------------------------------------------------------------------
## 0. Headline

1. Both keep a table's identity apart from its name and its location. A stable id keys the row, the location is an attribute
   of that row, and a rename rewrites name and parent only. Neither has a location hand-over between ids.
2. Neither enforces location exclusivity with a unique constraint. Lakekeeper reads under a transaction-scoped advisory lock
   per path prefix and relies on READ COMMITTED (lakekeeper@5355a1b:PG/tabular/mod.rs:523-560, 601-689). Polaris reads in the
   service layer and writes in a separate call; the project's own issue calls that racy and is open (polaris#2295).
3. Lakekeeper describes the end state LH-458 reproduces, reached by a different trigger (two concurrent creates):
   lakekeeper#2005 (merged 2026-09-01) says they "could both pass the check ... leaving one tabular's data inside another's
   location, where purging either destroys the other's files", and that "A unique index cannot express this: the colliding
   values differ".
4. DROP against RENAME. Lakekeeper: both statements `SELECT ... FOR UPDATE` the same `tabular` row, and the rename's WHERE
   re-checks name, namespace and `deleted_at IS NULL`; a rename behind a committed drop finds no row and answers 404.
   Polaris on JDBC: rename is a one-row version compare-and-swap, drop is an unconditional delete by id; a rename behind a
   drop answers 404 or 503, a drop behind a rename deletes the renamed row.
5. Dropped, not purged. Lakekeeper's soft delete keeps the row and, because the location check has no `deleted_at` filter,
   the location; undrop clears `deleted_at`. Polaris has no soft delete (polaris#5054 proposes one): the row is deleted at
   drop and the location is free at once.
6. Both free the location in the catalog before the bytes are touched. Lakekeeper's purge worker deletes the location prefix
   from its payload without reading the catalog again; Polaris's cleanup deletes the files the dropped table's last metadata
   names, not a prefix.
7. Defaults. Lakekeeper's default location is `<namespace location>/{uuid}`, unique by construction. Polaris's is
   `<namespace location>/<table name>`, so drop then create of one name reuses the directory, unless
   `DEFAULT_UNIQUE_TABLE_LOCATION_ENABLED` (default false, polaris#4606) adds a random suffix.

---------------------------------------------------------------------------------------------------------
## 1. Lakekeeper (lakekeeper@5355a1b)

### 1.1 Identity and where the location lives
- Identity is `tabular_id` (UUID), primary key `(warehouse_id, tabular_id)` (crates/lakekeeper-storage-postgres/migrations/
  20250904142650_reusable_table_id.sql:242; ids repeat across warehouses by design, :2-3). The UUID is the Iceberg metadata's
  own: create passes `id: table_metadata.uuid()` (lakekeeper@5355a1b:PG/tabular/table/create.rs:46-49); a commit assigning
  another uuid is refused (lakekeeper@5355a1b:LKS/server/commit_tables.rs:126-135).
- The name is a mutable column under `UNIQUE NULLS NOT DISTINCT (warehouse_id, namespace_id, name, deleted_at)` (same
  migration :334), so a soft-deleted name is reusable (test lakekeeper@5355a1b:PG/tabular/table/mod.rs:2154-2210).
- The location is columns `fs_protocol`, `fs_location` of the same row (lakekeeper@5355a1b:PG/tabular/mod.rs:715), stored
  without scheme or trailing slash (:562-576; CHECK in migrations/20260830000000_normalize_and_index_tabular_location.sql:17-19).
- A table's location is immutable: `SetLocation` to another value answers `SetLocationNotAllowed`
  (lakekeeper@5355a1b:LKS/server/commit_tables.rs:136-151). A view may move, and its commit re-runs the location check
  (lakekeeper@5355a1b:PG/tabular/view/mod.rs:144). The default location is `<namespace location>/{uuid}`
  (lakekeeper@5355a1b:LKS/service/storage/storage_layout.rs:443-447); a caller may name one inside the warehouse base
  (lakekeeper@5355a1b:LKS/server/tabular.rs:307-309).

### 1.2 Exclusivity on create, register, rename, update
- One function, `ensure_location_available` (lakekeeper@5355a1b:PG/tabular/mod.rs:611-689), called from `create_tabular`
  (:709) and from view commit. `create_tabular` serves table create, staged create and register including overwrite
  (lakekeeper@5355a1b:LKS/server/tables.rs:510 -> PG/catalog.rs:255-260 -> PG/tabular/table/create.rs:46), view create
  (PG/tabular/view/mod.rs:44) and generic tables (PG/tabular/generic_table.rs:99). Rename does not call it (the location does
  not move), nor does a table commit (immutable). Register with `overwrite` deletes the previous row in the same transaction
  first (LKS/server/tables.rs:497-506).
- It compares, inside one `warehouse_id`, every other tabular (`tabular_id != self`) whose `fs_location` equals the candidate
  or one of its ancestor paths, or lies under it (byte range `[X/, X0)`) (:660-673). Exact, ancestor and descendant; whole
  warehouse, not siblings only; tables, views and generic tables share one space (lakekeeper@5355a1b:crates/lakekeeper-
  integration-tests/tests/generic_table_location_collision.rs:1-7); staged rows count (tests/server_tables_postgres.rs:1714-1812).
- Not optional: I found no configuration switch for it (crates/lakekeeper/src/config.rs has no location setting;
  docs/docs/configuration.md lists none). The refusal is HTTP 409 `LocationAlreadyTaken`.
- Race-freedom comes from locking, not a constraint. The only location indexes are non-unique
  (migrations/20250904142650_reusable_table_id.sql:328, 20260830000000_normalize_and_index_tabular_location.sql:4-7; the file
  `20240911094637_unique_tabular_location.sql:5` also creates a non-unique index). The check first takes
  `pg_advisory_xact_lock` on one key per path prefix of the candidate that contains a `/`, `xxh3(warehouse_id, 0, prefix)`,
  sorted and de-duplicated against deadlock (:545-560, 619-632). Two colliding paths share a key because the shorter full
  path is among the longer one's prefixes (:526-529), so they serialise; the doc comment says this "relies on READ
  COMMITTED: the check must take its snapshot after the lock wait to see the holder's committed row" (:604-607).
  `begin_write` sets no isolation (lakekeeper@5355a1b:PG/lib.rs:93-94) (inference: the server default applies). The unit test
  asserts the key sets overlap for parent and child, not a live race (:2176-2255). The bare authority is left out of the keys
  so a bucket's creates do not serialise (:534-539).
- A warehouse's own storage location is checked against the project's other warehouses by a list-then-insert
  (lakekeeper@5355a1b:LKS/api/management/v1/warehouse/mod.rs:2417-2436, LKS/service/storage/s3.rs:299-323); I found no lock
  or unique constraint behind it (the only advisory locks are the tabular location, grants and the OpenFGA reconcile).

### 1.3 DROP against RENAME of one table
- Both handlers resolve the table by name in a read outside the write transaction (lakekeeper@5355a1b:LKS/server/tables.rs:882,
  LKS/server/tables/rename_table.rs:190-193), open the transaction, then act on the id (tables.rs:907, rename_table.rs:116).
- Rename: CTE `locked_tabular` selects the row `WHERE tabular_id AND typ AND deleted_at IS NULL AND namespace_id = <resolved>
  AND name = <resolved> FOR UPDATE`, then `UPDATE tabular SET name = $1 FROM locked_tabular`
  (lakekeeper@5355a1b:PG/tabular/mod.rs:1438-1473; cross-namespace variant also locks both namespaces, :1558-1594). The
  comment: the row "must still be the one the caller resolved", or "a rename that lost a race" would act "on whatever the
  winner left behind" (:1446-1449, 1567-1572).
- Drop: soft = `FOR UPDATE` on `(warehouse_id, tabular_id)` then `SET deleted_at` (:1904-1927); hard = `FOR UPDATE` then
  `DELETE FROM tabular` (:2024-2041). Neither predicates on name.
- Outcome (inference from the SQL plus PostgreSQL's READ COMMITTED recheck of a blocked statement's WHERE, which is not a
  repo statement): drop commits first, the rename's CTE yields no row, `fetch_one` gives `RowNotFound`, mapped to
  `TabularNotFound` (:1391-1408) and HTTP 404 `NoSuchTableException` (LKS/service/catalog_store/tabular.rs:1203-1218).
  Rename commits first, the drop locks the same id and removes the table under its new name; id and location were never
  separated. No test races a drop against a rename; the stale-rename tests are rename against rename and sequential
  (PG/tabular/table/mod.rs:1654-1800).
- A rename request carrying an idempotency key inserts it in the rename's transaction, so a committed key implies a
  committed rename; a concurrent holder of the key gets `request_in_progress` (LKS/server/tabular.rs:23-61).

### 1.4 Drop semantics and undrop
- The warehouse's delete profile is `Hard` or `Soft{expiration}`; `force` forces hard (LKS/server/tables.rs:909-914).
  Soft schedules a `soft_deletion` task for `now + expiration`, then sets `deleted_at` in one transaction (:944-977).
- A soft-deleted tabular keeps its location: the check has no `deleted_at` predicate (PG/tabular/mod.rs:660-673) (inference
  from the SQL; no test). Undrop sets `deleted_at = NULL` under `FOR UPDATE`; its only conflict is the name,
  `unique_name_per_namespace_id` -> `TabularAlreadyExists` (:1759-1782, 1865-1869); it runs no location check.
  Docs: a soft-deleted table still blocks deleting its namespace and warehouse (docs/docs/concepts.md:117, 152-160).
- Expiry: one database transaction hard-deletes the row (`drop_tabular(..., true)`) and, for a purge, enqueues
  `tabular_purge` with the location string, then commits (LKS/service/tasks/tabular_expiration_queue.rs:176-201, 296-322); the
  authorisation tuples are deleted by a call whose error is ignored (:220-228). A hard drop enqueues the purge the same way
  (LKS/server/tables.rs:917-943).
- The purge worker runs `remove_all(file_io, location)` from the payload and reads only the warehouse
  (LKS/service/tasks/tabular_purge_queue.rs:146-226, 214). It does not consult the catalog for a tabular now at that location
  (this file only). The default `{uuid}` directory is what keeps a later create off it; a caller-named location bypasses that
  (inference; not reproduced). docs/docs/concepts.md:150: purge removes "all files of the table".
- docs/docs/storage.md:43: vended credentials are downscoped to the table's location and "no two table locations in a
  Warehouse overlap".

### 1.5 Gaps and races the project states
- PG/tabular/mod.rs:601-607: "no index on `fs_location` is unique, so nothing at the schema level backstops it."
- lakekeeper#321 (closed 2024-09-18): two tables on a location hand out credentials that work for both; a comment by c-thiel:
  "Unique constraint won't quite be enough, we also need to make sure no table is created in a parent folder."
- Closed false negatives: an unescaped `LIKE` read a backslash as a pattern; a trailing slash matched neither comparison
  (lakekeeper#2005, tests PG/tabular/mod.rs:2257-2469); a location that is a string prefix of another without `/` was
  refused (lakekeeper#779, closed 2025-01-23). lakekeeper#1745 (unique index on canonical `fs_location`) was closed unmerged
  one day after opening; the PR text gives no reason; HEAD has no such index.
- Pre-existing duplicates are reported, not repaired: the 20260830 migration aborts only if trimming would create a shared
  location, and merely warns for locations already shared ("move or deregister rather than purge")
  (PG/migrations/normalize_fs_location.rs:64-104, 106-138).
- A staged row (`metadata_location IS NULL`) holds its location; the only removal I found is a same-name create
  (PG/tabular/table/create.rs:166-205); `TabularListFlags::active()` excludes it, so `drop_table` cannot see it (inference).

---------------------------------------------------------------------------------------------------------
## 2. Apache Polaris (polaris@1a8dfd7)

In this section a bare `LocalIcebergCatalog.java:n` is PSVC/catalog/iceberg/LocalIcebergCatalog.java, a bare
`AtomicOperationMetaStoreManager.java:n` is PCORE/persistence/AtomicOperationMetaStoreManager.java, bare
`JdbcBasePersistenceImpl.java`, `QueryGenerator.java`, `DatasourceOperations.java` are in PJDBC, `MutationAttempt.java` is
PNOSQL/mutation/MutationAttempt.java, and `TransactionalMetaStoreManagerImpl.java`, `TreeMapMetaStore.java`,
`TreeMapTransactionalPersistenceImpl.java` are in PCORE/persistence/transactional.

### 2.1 Identity and where the location lives
- Identity is the numeric entity `id` within `(realm, catalog)`, with `parentId`, `typeCode`, `name`, `entityVersion`
  (polaris@1a8dfd7:PCORE/entity/PolarisEntityCore.java:33-49; ids from `IdGenerator`, PJDBC/JdbcBasePersistenceImpl.java:123-126).
  JDBC row: PK `(realm_id, id)`, `UNIQUE (realm_id, catalog_id, parent_id, type_code, name)`, `entity_version`,
  `location_without_scheme` (PJRES/postgres/schema.sql:34,45-47).
- A table-like entity's location is its `location` property (PCORE/entity/PolarisEntityConstants.java:23;
  PCORE/entity/table/IcebergTableLikeEntity.java:118-120); the metadata file path is an internal property (:105-107). Rename
  rewrites name and parent and carries the properties, bumping `entity_version`
  (AtomicOperationMetaStoreManager.java:1075-1101). Unlike Lakekeeper, a commit may change the location, and the overlap check
  re-runs when it does (LocalIcebergCatalog.java:2013-2018, 2121-2134).

### 2.2 Exclusivity: flags, scope, and what makes it race-free
- Flags, all in polaris@1a8dfd7:PCORE/config/FeatureConfiguration.java: `ALLOW_TABLE_LOCATION_OVERLAP` false (:284-295,
  catalog key `polaris.config.allow.overlapping.table.location`), `ALLOW_NAMESPACE_LOCATION_OVERLAP` false (:297-304),
  `ALLOW_UNSTRUCTURED_TABLE_LOCATION` false (:325-337), `OPTIMIZED_SIBLING_CHECK` false (:684-696, "Supported by the JDBC and
  NoSQL metastore implementations"), `ALLOW_OPTIMIZED_SIBLING_CHECK` false (:671-682, startup acknowledgment,
  PSVC/config/ProductionReadinessChecks.java:559-578), `DEFAULT_UNIQUE_TABLE_LOCATION_ENABLED` false (:711-721),
  `ALLOW_CLIENT_SPECIFIED_TABLE_LOCATION` true (:359-371), `DROP_WITH_PURGE_ENABLED` false (:442-451).
- Where: `LocalIcebergCatalog.validateNoLocationOverlap`, skipped when `ALLOW_TABLE_LOCATION_OVERLAP`
  (LocalIcebergCatalog.java:1426-1457), called on create and register (via `validateTableMetadataLocations`, :1390-1405), on a
  commit that changes locations (:2015-2018), on stage-create (:1326-1340) and for namespaces (:718, :930). Rename calls none
  (:651-657, 2760-2866).
- Default scope is siblings only: it lists the parent's namespaces and, under a namespace, its tables, resolves them, and
  refuses a candidate that `isChildOf` a sibling or the reverse, or equals an ancestor namespace's location
  (:1563-1604, 1638-1670; `StorageLocation.isChildOf` slash-terminates both, PCORE/storage/StorageLocation.java:129-137). That
  suffices only because, without `ALLOW_UNSTRUCTURED_TABLE_LOCATION`, a table location must lie under its nearest ancestor's
  `location` (PCORE/storage/PolarisStorageConfigurationInfo.java:139-148); the code calls siblings-only plus unstructured
  locations "not a safe combination" (:1293-1302).
- With `OPTIMIZED_SIBLING_CHECK` the scope is the catalog: equal, ancestor and descendant, through
  `hasOverlappingSiblings` (:1539-1560; contract PCORE/persistence/BasePersistence.java:400-425). JDBC builds an OR of equality
  terms per prefix and a `LIKE` over `location_without_scheme` (QueryGenerator.java:386-450) on the non-unique
  `idx_locations (realm_id, catalog_id, location_without_scheme)` (PJRES/postgres/schema.sql:53-55); beyond 40 `/` components
  it falls back to the sibling path (JdbcBasePersistenceImpl.java:95, 800-803). NoSQL walks a locations index
  (PNOSQL/NoSqlMetaStore.java:632-760); the in-memory store scans all entities (TreeMapTransactionalPersistenceImpl.java:676-708).
- Race-freedom: none, in any backend. The check is a plain read; the write is a separate call. JDBC: SELECT
  (JdbcBasePersistenceImpl.java:805-810) then an INSERT in its own autocommit statement (DatasourceOperations.java:253-272),
  unique only on names. NoSQL: the commit keeps the locations index as a set of entity ids per location
  (MutationAttempt.java:541-584) and refuses nothing about overlap. In-memory: a global lock, but the check runs in its own
  transaction (TransactionalMetaStoreManagerImpl.java:2324-2336; TreeMapMetaStore.java:196-197, 423-426). The CAS comment at
  LocalIcebergCatalog.java:2038-2041 concerns the same entity's metadata location, not overlap (inference).
  polaris#2295 (open since 2025-08-07, filed by snazy): "it is possible to create multiple tables with the same base location",
  because the check "performs 1 or many individual calls to `PolarisMetaStoreManager`" each in its own isolation, so "even
  serializable isolation would not help here"; a unique constraint on `location_without_scheme` was suggested and not taken
  because "that seems to conflict with ALLOW_*_OVERLAP".

### 2.3 DROP against RENAME, by backend
- Atomic manager (`relational-jdbc`, `in-memory-atomic`): one autocommit statement per write
  (AtomicOperationMetaStoreManager.java:85-87; DatasourceOperations.java:253-272). Rename: read by id, version compare, read of
  the destination name, then `UPDATE ... WHERE id AND entity_version AND grant_records_version` (:1004-1107;
  JdbcBasePersistenceImpl.java:234-257). Drop: read by id, then `DELETE ... WHERE realm, catalog, id` with no version
  (:1129-1136, 197; JdbcBasePersistenceImpl.java:350-368), then grant records and policy mappings removed best effort
  (:199-265). Create: `INSERT`; a unique-key violation is read back, the same id being an idempotent retry and another id
  `ENTITY_ALREADY_EXISTS` (:106-139; JdbcBasePersistenceImpl.java:201-233). The catalog path is not re-checked on any of
  the three (TODOs :891-895, 1103-1105, 1123-1126).
- Transactional manager (`in-memory`, test only): each call runs under a global lock with rollback and first re-resolves the
  catalog path and the entity (TransactionalMetaStoreManagerImpl.java:1197-1304, 1339).
- NoSQL (`nosql`: MongoDB, DynamoDB): create, rename and drop are updates applied inside one commit on the catalog's
  reference. Rename compares the version, checks the new name and moves the location-index entry (MutationAttempt.java:308-441);
  drop compares the version and removes the id, name and location entries (:161-306); a parent id must exist in the commit's
  own index (:586-598).
- JDBC loser: drop first -> rename's read returns null -> `ENTITY_NOT_FOUND` -> 404; drop between rename's read and its CAS -> zero rows
  -> `RetryOnConcurrencyException` -> `TARGET_ENTITY_CONCURRENTLY_MODIFIED` -> 503 with `Retry-After: 3`, and the retry meets 404
  (LocalIcebergCatalog.java:2826-2849). Rename first -> drop deletes the renamed row by id and succeeds. The CAS includes
  `grant_records_version`, so a concurrent grant also fails a rename (JdbcBasePersistenceImpl.java:235-236). The mapping is
  tested with a mocked manager status, not a race (runtime/service/src/test/java/org/apache/polaris/service/catalog/iceberg/
  AbstractLocalIcebergCatalogTest.java:2990-3039).
- NoSQL loser: a rename behind a drop finds the id absent from the index and answers `ENTITY_NOT_FOUND` (MutationAttempt.java:
  327-330); a drop behind a rename finds the version moved and answers `TARGET_ENTITY_CONCURRENTLY_MODIFIED` (:198-201). The
  committer "performs an atomic change against a named reference", with an attempt that
  "must be side-effect-free and prepared to be called multiple times"
  (persistence/nosql/persistence/api/src/main/java/org/apache/polaris/persistence/nosql/api/commit/Committer.java:29-31, 62-66).

### 2.4 Drop semantics
- Hard delete: "we will fully delete the entity from persistence store" (AtomicOperationMetaStoreManager.java:169-182); no
  soft delete, hold or undrop for tables (searched the tree for undrop and restore: none; polaris#5054, open, describes
  today's drop as having "no first-class undrop or hold period"). The location is free as soon as the row is gone.
- Cleanup (purge requested and `DROP_WITH_PURGE_ENABLED`, LocalIcebergCatalog.java:3025-3041) is a TASK entity created after
  the delete: JDBC as a separate call (:1240-1266, TODO "possible that the entity is dropped but we don't have any persisted
  task records"); NoSQL as a second commit whose failure is logged WARN (PNOSQL/NoSqlMetaStore.java:454-469).
- The handler reads the dropped table's last metadata file and schedules per-file deletion of manifests, data files and
  metadata files (PSVC/task/TableCleanupTaskHandler.java:53-56, 98-130; PSVC/task/ManifestFileCleanupTaskHandler.java:44-48),
  not a prefix.
- Default location `<namespace location>/<table name>` (LocalIcebergCatalog.java:528-557) means drop then create of one name
  reuses the directory; polaris#4606 (merged 2026-07-15) added the unique suffix flag, default off.

### 2.5 Known gaps the project states
- Code TODOs in AtomicOperationMetaStoreManager.java: no catalog-path re-validation on create, contradicting the interface
  contract "we will re-resolve the specified path" (:891-895; PCORE/persistence/PolarisMetaStoreManager.java:238-253); rename
  needs "post-validation of source and destination parent paths" (:1103-1105); drop concurrent with parent-path changes
  (:1123-1126); a namespace drop can delete children created in the race window (:1161-1167); drop and cleanup-task creation
  are not atomic (:1259-1262). NoSQL: grant and policy cleanup is separate and stale entries are tolerated
  (PNOSQL/NoSqlMetaStore.java:625-628).
- polaris#1389 (open): concurrent create and delete answered 500 from sibling resolution. polaris#4407 (closed) was the same
  500; its fix #4408 (merged 2026-05-20) tolerates siblings deleted mid-check and answers 409. #4408: a test for it "cannot
  produce true positive results".
- CHANGELOG.md:130-135, 211-214, 391-402, 794-797 record: the default check accepted a location equal to its namespace's, the
  optimized check rejected every entity, a JDBC false negative on slash-less ancestors, a mis-ordered index, and a warning not
  to enable `OPTIMIZED_SIBLING_CHECK` on a realm without the `location_without_scheme` backfill.
- CVE-2026-42809 (< 1.4.1): stage-create issued credentials before location validation, before "the effective table location has
  been validated or durably reserved" (site/content/community/security-advisories/CVE-2026-42809.md:41-43). The staged handler
  now validates and persists no entity (PSVC/catalog/iceberg/IcebergCatalogHandler.java:711-725) (inference: no durable
  reservation).
- Backends: JDBC is "Ready to use" and PostgreSQL or CockroachDB only; in-memory is test only; NoSQL is experimental;
  `persistence/dynamodb-atomic` is work in progress; EclipseLink is removed (site/content/in-dev/unreleased/metastores/_index.md:
  32-39, relational-jdbc.md:29, persistence/dynamodb-atomic/README.md:22, CHANGELOG.md:682).

---------------------------------------------------------------------------------------------------------
## 3. Side by side

| | Lakekeeper | Polaris (JDBC) |
|---|---|---|
| table identity | UUID from the table metadata | numeric entity id, versioned |
| location moves on rename | no | no |
| location mutable | views only | yes, check re-runs |
| exclusivity check | exact, ancestor, descendant, whole warehouse | siblings only, or catalog-wide with the optimized flag |
| optional | no | yes: `ALLOW_TABLE_LOCATION_OVERLAP` |
| atomic with the write | yes: advisory locks, one transaction | no |
| unique index on location | no | no |
| drop vs rename arbiter | row lock on the one row | version CAS against an unconditional delete |
| dropped keeps location | soft: yes; hard: no | no |
| catalog frees location before bytes | yes | yes |

---------------------------------------------------------------------------------------------------------
## 4. What rask's store can and cannot borrow

Constraints, from records.py and location_claims.py: per key the store gives create-if-absent (`If-None-Match: *`,
packages/service-kit/src/service_kit/lakehouse/records.py:89-105), replace-if-ETag (`If-Match`, :235-249) and read with ETag
(:158); delete is unconditional (record_store.py:84-88); nothing spans two keys; no lock with an owner or expiry; a rename makes
a new id, so it is a two-record change (manifest and claim); a claim is keyed by the sha256 of the decoded path (location_claims.py:92).

- Identity. Both projects keep the id and the location on one row and rename only the name, so no arbiter ever has to move a
  location between ids. rask's id is the name path, and the claim hand-over (location_claims.py:106-158) has no counterpart.
- Uniqueness primitive. A create-if-absent record per location is the object-store form of the unique index neither project
  has. It sees equal locations only. Both projects also refuse ancestor and descendant locations, and Lakekeeper notes a unique
  index "cannot express" parent and child (lakekeeper#2005; PG/tabular/mod.rs:2190-2193); whether rask's Lance directories
  nest was not examined.
- Check against write. Lakekeeper makes check and insert one locked transaction; Polaris does not, and polaris#2295 calls it racy.
  rask's take is the write itself (a create-if-absent), so the take has no check-then-write gap of the kind polaris#2295
  describes. rask's `create` door holds no claim and leans on the register door's after-commit manifest check
  (location_claims.py:21-24); neither project checks after the commit.
- Locks. Lakekeeper's `pg_advisory_xact_lock` has no object-store counterpart in rask's two primitives. A create-if-absent
  record used as a mutex needs an owner and an expiry for a crashed holder; `LEASE` (location_claims.py:53) is that device for claims.
- Optimistic CAS. Polaris's `entity_version` CAS and Lakekeeper's `metadata_location IS NOT DISTINCT FROM old` CAS
  (lakekeeper@5355a1b:PG/tabular/table/commit.rs:289) match replace-if-ETag on one key.
- Release. Polaris JDBC deletes after a read with no version (JdbcBasePersistenceImpl.java:350-368), the shape of LH-458, but
  the deleted row is the identity, not the exclusivity arbiter; rask's `release` deletes the arbiter after a read
  (location_claims.py:161-170). In the drop paths I read, Lakekeeper's lock, predicate and delete are one statement
  (PG/tabular/mod.rs:2024-2041). Replacing the claim under `If-Match` puts release and hand-over on one arbiter, the property
  both projects' drop-against-rename rest on. Neither project writes a "released" marker in the drop paths I read;
  Lakekeeper's nearest form is the soft-deleted row, which keeps holding.
- Drop against rename. Both serialise on the one row both operations touch, and the loser sees the post-state (404, or 503
  retry). In rask the claim key is the only record both touch.
- Soft drop. Lakekeeper holds the location through the soft delete and needs no check on undrop. rask's recoverable drop does
  the same through the trash claim (table_claims.py:81-91).
- Bytes. Both free the catalog's hold before deleting bytes. Lakekeeper's purge does not re-read the catalog; Polaris deletes
  named files. rask's destructive drop refuses while another live id holds the location (table_claims.py:93-110).
- Stale holders. Lakekeeper's staged row and Polaris's non-persisting stage-create are two answers to a holder that never
  attaches; Lakekeeper's has no reaper that I found, rask has `is_gone` plus `LEASE`.

---------------------------------------------------------------------------------------------------------
## 5. Not confirmed

- A real concurrent drop against rename in either project: no test does it. Lakekeeper's outcome is read from the SQL and
  PostgreSQL's READ COMMITTED recheck of a blocked statement's WHERE (PostgreSQL's manual, outside both repos, not opened);
  Polaris's from the Java and SQL. Looked in: PG/tabular/table/mod.rs tests, crates/lakekeeper-integration-tests/tests/
  {soft_deletion,server_views_rename,server_views_drop,service_tabular_expiration_queue,openfga_rename_tabular}.rs, and
  AbstractLocalIcebergCatalogTest.java.
- Lakekeeper's session isolation: no `isolation` or `default_transaction` setting in crates/lakekeeper-storage-postgres/src; the
  rest of `build_connect_ops` (PG/lib.rs:346-380) and the Postgres server default were not read.
- `remove_all` itself (the `lakekeeper-io` storage backends): recursion is taken from its name and docs/docs/concepts.md:150;
  whether any caller re-checks the catalog before purging: only tabular_purge_queue.rs read.
- Whether any Lakekeeper process reaps a staged row that is never finalised: searched LKS/service/tasks/*.rs and docs/docs/*.md
  for "staged", found nothing.
- Polaris NoSQL: the `memoizedIndexedAccess` staleness bound, the committer retry loop beyond its API comment, and the
  MongoDB and DynamoDB conditional-write behaviour. Polaris's `Resolver` and the cache layer were not read.
- Polaris JDBC: `runWithinTransaction` (JdbcBasePersistenceImpl.java:154, 406, 1062) backs the batch writes and was not traced
  for rename or drop; the isolation level (CockroachDB defaults to SERIALIZABLE, JdbcBasePersistenceImpl.java:221) is judged
  only through polaris#2295's statement that it would not help.
- Polaris `docs/` and `site/content/in-dev/unreleased/` are near-duplicates; the site copy is cited.
- Issue text was read for lakekeeper#321, #779, #1745, #2005 and polaris#1389, #2295, #4407, #4408, #4606, #5054 only; the
  clones are `--depth 1`, so no blame or history was available. Mailing-list threads linked from polaris#4606 and #5054 were not opened.
- Whether the owner's rask estate nests Lance dataset directories (decides whether exact-path claims suffice): not examined.

---------------------------------------------------------------------------------------------------------
## Files read

Lakekeeper: PG/tabular/{mod.rs (500-790, 1340-2130, 2133-2530), table/{create,commit,mod}.rs, view/mod.rs, load_by_location.rs},
PG/{advisory_lock,lib,catalog}.rs, PG/migrations/normalize_fs_location.rs, four migrations (20240911, 20250904, 20260318, 20260830),
LKS/server/{tables.rs, tables/rename_table.rs, commit_tables.rs, tabular.rs}, LKS/service/tasks/{tabular_expiration_queue,
tabular_purge_queue}.rs, LKS/service/storage/{storage_layout,s3}.rs, the two collision integration tests, docs/docs/{storage,concepts}.md.
Polaris: PCORE/persistence/{AtomicOperationMetaStoreManager, PolarisMetaStoreManager, BasePersistence, transactional/*}.java, PJDBC
(JdbcBasePersistenceImpl, QueryGenerator, DatasourceOperations), PJRES/postgres/schema.sql, PNOSQL (NoSqlMetaStore,
NoSqlMetaStoreManager, mutation/MutationAttempt), PSVC/catalog/iceberg/{LocalIcebergCatalog, IcebergCatalogHandler}.java,
PSVC/task/TableCleanupTaskHandler.java, PCORE/config/FeatureConfiguration.java, CHANGELOG.md, SECURITY-THREAT-MODEL.md, the
metastores and flags pages under site/content/in-dev/unreleased/. rask: records.py, record_store.py, location_claims.py, table_claims.py.
