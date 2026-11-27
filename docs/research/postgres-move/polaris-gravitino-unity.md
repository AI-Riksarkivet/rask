# Apache Polaris, Apache Gravitino, Unity Catalog (open source): design, and what each does for a Lance table

Read on 2026-11-26 from the default branch of each repository (tarball snapshots), not from memory.

| Repository | Commit read | Commit date |
| --- | --- | --- |
| `apache/polaris` | `7144ebfc0ea77226ce89024f51ffbd4d942e6300` | 2026-11-26 |
| `apache/gravitino` | `0709ea686e253174e975fadba4ccadb7a14b033b` | 2026-11-26 |
| `unitycatalog/unitycatalog` | `f59e83d35fcd4a99415711d0d6551325a156f43c` | 2026-11-26 |
| `lance-format/lance-namespace` | `a13c746bdf369b62d4fc583357564f0cddfad667` | 2026-10-23 |
| `lance-format/lance-namespace-impls` | `f815e62b1d64f620ce6f1219813d338a3dd7f04a` | 2026-10-10 |

Documentation was read from each repository's own docs tree (Polaris `site/content/in-dev/unreleased/`, Gravitino
`docs/`, Unity `docs/`), which is the source of the published sites. The published sites themselves were not fetched.
Paths below are relative to the repository named in the section. "Doc" marks a claim read in documentation only;
"code" marks one read in source. "Inferred" marks a conclusion of mine.

The short version of the Lance comparison is in section 4; the agreement table is section 5.

---

## 1. Apache Polaris

**What it is.** A catalog server for Apache Iceberg tables (it implements the Iceberg REST catalog), with its own
role-based access control, storage credential vending, and a second, small API for "generic tables" in other formats.

### 1.1 State and transactions

**How it is built.**
- All catalog and governance state is in one metastore. Doc, `site/content/in-dev/unreleased/metastores/_index.md`:
  the metastore holds "entities: catalogs, namespaces, tables, views, principals, roles", "RBAC data" and "policies".
  Backends: in-memory (test only), PostgreSQL through JDBC ("Recommended for production"), MongoDB, DynamoDB
  (experimental).
- Code, `persistence/relational-jdbc/src/main/resources/postgres/schema.sql`: tables `entities`, `grant_records`,
  `principal_authentication_data`, `policy_mapping_record`, `events`, `scan_metrics_report`, `commit_metrics_report`.
  Every table is keyed by `realm_id` ("realm_id used for multi-tenancy").
- Concurrency is a compare-and-swap on a version column inside a database transaction. Code,
  `persistence/relational-jdbc/.../JdbcBasePersistenceImpl.java` line 213: "CAS on both entity_version and
  grant_records_version because grant operations only bump grant_records_version without touching entity_version."
- For an Iceberg table the catalog holds the commit pointer (the table entity names the current metadata file), so an
  Iceberg commit is a metastore write. For a generic table it holds no pointer (section 1.8).
- Side effects outside the store are not transactional with it, and the code says so:
  - Dropping with purge writes a task entity in the same metastore. `polaris-core/.../persistence/AtomicOperationMetaStoreManager.java`
    line 1262: "TODO: Add a way to create the task entities atomically with dropping the entity; in the meantime, if
    the server fails partway through a dropEntity, it's possible that the entity is dropped but we don't have any
    persisted task records that will carry out the cleanup."
  - Grant cleanup on drop is "Best-effort cleanup" (same file, line 200).
  - Events are "not written in the same transaction as the operation they describe" (doc, `events.md`).

**Conceptually strong.** One store, one CAS rule, realm in every key. The drop-then-task ordering is at least written
down as a known gap.

**Weak or absent.** No outbox: an event or a cleanup task can be lost after the metastore write commits. A relational
or NoSQL database is mandatory.

### 1.2 Authorization

**How it is built.**
- Model (doc, `managing-security/access-control.md`): privileges are granted to **catalog roles**, catalog roles to
  **principal roles**, principal roles to **principals**. Securables: catalog, namespace, Iceberg table, view, policy.
  Table privileges separate metadata from data: `TABLE_READ_DATA` "Enables reading data from the table by receiving
  short-lived read-only storage credentials from the catalog"; `TABLE_WRITE_DATA` the read+write equivalent.
- Checks happen in the request handler before the catalog is touched (code, `runtime/service/.../catalog/common/CatalogHandler.java`:
  "Initialize the catalog once authorized. Called after all `authorize...` methods").
- The authorizer is an SPI: `polaris-core/.../auth/PolarisAuthorizer.java` (`resolveAuthorizationInputs`, `authorize`,
  and a batch `authorize` that "must return a decision for every intent and must not short-circuit across intents").
  Selected by `polaris.authorization.type`, default `internal`. Shipped implementations: internal RBAC, Open Policy
  Agent (`extensions/auth/opa`, documented as "currently a preview feature"), Apache Ranger (`extensions/auth/ranger`).
  With OPA, Polaris POSTs actor, action and resource to `polaris.authorization.opa.policy-uri` and enforces the
  boolean (doc, `managing-security/external-pdp/opa.md`).
- Listing: authorized on the container. Per-entity filtering exists only behind a flag and only in the Iceberg
  handler: code, `runtime/service/.../catalog/iceberg/IcebergCatalogHandler.java` line 1987
  (`authorizeListOrRequireFiltering` reads `ENABLE_ENTITY_LEVEL_LIST_FILTERING`; when the container check is denied
  and the flag is on, each candidate is checked with `LIST_NAMESPACE` / `LIST_TABLE` / `LIST_VIEW`).

**Conceptually strong.** A narrow authorizer SPI with a batch entry point, so an external engine can decide every
operation and filter a list in one call. Data access is a separate privilege from metadata access.

**Weak or absent.** OPA is preview. Entity-level list filtering is opt-in and not applied to generic tables
(`GenericTableCatalogHandler.listGenericTables` calls `authorizeBasicNamespaceOperationOrThrow` and returns the page).
With an external authorizer, its decision log is that system's, not Polaris's (doc, `events.md`).

### 1.3 Storage access

**How it is built.**
- Vending is per table load, short-lived, scoped. Doc, `vended-credentials.md`: "Polaris calls AWS STS `AssumeRole`
  with an inline session policy scoped to the specific table locations and operations (read, list, write) the caller
  is authorized to perform." Azure: user delegation SAS scoped to container and path prefix. GCS: downscoped token
  through a Credential Access Boundary. Code: `polaris-core/.../storage/aws/AwsCredentialsStorageIntegration.java`
  builds an `IamPolicy` with `s3:GetObject`, `s3:ListBucket`, `s3:PutObject` per location (lines 304-377) and passes
  it to STS with `durationSeconds`.
- Revocation is at the next vend: "Polaris checks the principal's current table privileges whenever a client requests
  or refreshes vended credentials"; a credential already held stays valid until it expires (same doc).
- Location overlap: the `entities` table carries `location_without_scheme` with an index
  (`schema.sql` lines 45-55). Doc, `generic-table.md`: "With `ALLOW_TABLE_LOCATION_OVERLAP=false` (the default), a
  provided location must not overlap an existing table or namespace location ... Equal locations and parent/child
  locations are conflicts".

**Conceptually strong.** Credential scope is derived from the authorization decision (read versus write) and the
table's own prefix; overlap is refused by default and indexed.

**Weak or absent.** Generic tables get none of the vending (section 1.8).

### 1.4 Events and audit

**How it is built** (doc, `events.md`, all quotes from it).
- `BEFORE_*` and `AFTER_*` events per operation, including `GENERIC_TABLE` ones (code,
  `runtime/service/.../events/PolarisEventType.java` lines 188-192).
- Listeners: the metastore's `events` table, AWS CloudWatch, Kafka, OpenTelemetry log records.
- Guarantee: "Polaris events are not a complete audit trail. They do not record authorization denials or
  authentication failures, and delivery is asynchronous and best-effort." "A failing listener loses the event."
  "Buffered events are lost if the process terminates abruptly."
- Data access is not in the events: "Catalog events do not record reads and writes to table data"; it is correlated
  through the cloud provider's access logs using STS session tags.
- Lineage: two extensions exist and are stubs. Code, `extensions/openlineage/.../NoOpOpenLineageIngestProvider.java`:
  "Accepts and discards every event. Persistence, dataset resolution, and downstream forwarding will land in follow-up
  PRs." `extensions/lineage/.../DefaultPolarisLineageHandler.java`: `throw new UnsupportedOperationException("Lineage
  query is not implemented yet.")`.

**Conceptually strong.** The documentation states the guarantee exactly, including what is lost.

**Weak or absent.** No at-least-once delivery, no denial events, no working lineage.

### 1.5 Background work and maintenance

**How it is built.**
- Polaris stores maintenance **policies** and executes none of them. Doc, `policy.md`: four system types,
  `system.data-compaction`, `system.metadata-compaction`, `system.orphan-file-removal`, `system.snapshot-expiry`, each
  a JSON document validated against a published schema, attachable to a catalog, namespace or table, inherited
  downward with nearest-wins. "Polaris stores and resolves these policies but does not run the maintenance operations
  itself. Compaction, snapshot expiration and orphan file removal are performed by external engines or services, for
  example Spark's Iceberg maintenance procedures." An engine reads them from
  `GET /polaris/v1/{prefix}/applicable-policies`.
- What Polaris itself does in the background: file deletion after a drop with purge (`runtime/service/.../task/TableCleanupTaskHandler.java`,
  an in-process executor that reads the dropped Iceberg table's metadata and deletes its files, with retries), and a
  Helm CronJob for its own NoSQL metastore's housekeeping (`helm/polaris/templates/cronjob-maintenance.yaml`, job
  `nosql-maintenance`).
- No soft delete and no undrop: a search of `polaris-core`, `runtime` and the docs for "undrop" and "soft delete"
  finds only `ENTITY_UNDROPPABLE` (an entity that may not be dropped).

**Conceptually strong.** A clean split: the catalog is where maintenance intent is declared, inherited and
authorized; engines act.

**Weak or absent.** Nothing checks that a policy is ever executed. No recovery from a mistaken drop.

### 1.6 Tenancy and configuration as code

- Hierarchy: realm → catalog → namespace (arbitrary depth) → table / view / generic table. A realm is "a logical
  partitioning mechanism ... enabling different teams, environments or organizations to operate independently within
  the same Polaris deployment", resolved from a request header (doc, `realm.md`).
- A catalog carries its storage configuration and allowed locations.
- Helm chart `helm/polaris` (Deployment, HPA, PodDisruptionBudget, HTTPRoute/Gateway/Ingress, ServiceMonitor,
  CronJob). No operator and no CRD in the chart's templates. Realms are bootstrapped with an admin tool
  (`admin-tool.md`: "`bootstrap` Bootstraps realms and root principal credentials"). Catalogs, roles, grants and
  policies are made through the management API; nothing in the chart declares them.

**Weak or absent.** No declarative (GitOps) form of catalogs, roles or grants.

### 1.7 Operations

Doc, `helm-chart/production.md`: stateless replicas behind a Service, `replicaCount` or an HPA; "This requires a
persistent metastore"; "all Polaris pods must share the same token signing keys". Shared between replicas: the
metastore and the signing keys.

### 1.8 What Polaris does for a Lance table

**What it is.** Polaris has a "generic table" API for non-Iceberg formats. Lance is not named in the Polaris product
documentation (`generic-table.md` gives "delta", "csv"); it is named in a Polaris blog post
(`site/content/blog/2026/01/06/lance-integration.md`: "enabling users to manage Lance tables through the Apache
Polaris Generic Table API"). The integration code lives in the Lance project, not in Polaris.

**How it is built.**
- A generic table is five fields: `name`, `format`, `base-location` (optional), `properties`, `doc`. Four
  operations: create, load, drop, list (`POST/GET/DELETE /polaris/v1/{prefix}/namespaces/{ns}/generic-tables`).
- The limitations, quoted in full from `generic-table.md`:
  1. "Generic tables provide limited spec information. For example, there is no spec for Schema or Partition."
  2. "There is no commit coordination provided by Polaris. It is the responsibility of the engine to coordinate
     loading and committing data. The catalog is only aware of the generic table fields above."
  3. "There is no update capability provided by Polaris. Any update to a generic table must be done through a drop
     and create."
  4. "Generic tables APIs do not support credential vending."
- Code confirms the shape: `runtime/service/.../catalog/generic/GenericTableCatalogHandler.java` has exactly
  `listGenericTables` (`LIST_TABLES`), `createGenericTable` (`CREATE_TABLE_DIRECT`), `dropGenericTable`
  (`DROP_TABLE_WITHOUT_PURGE`), `loadGenericTable` (`LOAD_TABLE`). Drop never purges.
- Location: "A location is not assigned automatically when this field is omitted"; a supplied one is checked against
  the catalog's allowed locations and the overlap rule, but "Generic Table locations are stored in internal
  properties and are outside the location index", so validation "can walk the catalog's namespaces".
- The Lance side (`lance-namespace-impls`, `docs/src/polaris.md` and `java/lance-namespace-polaris/.../PolarisNamespace.java`):
  a client-side adapter that maps Lance Namespace operations onto Polaris REST calls. A Lance table is "a Generic
  Table with `format` set to `lance`", location in `base-location`, `table_type=lance` in properties. Operations
  implemented: CreateNamespace, ListNamespaces, DescribeNamespace, DropNamespace (restrict only), DeclareTable,
  ListTables, DescribeTable, DeregisterTable (plus the exists calls). DeclareTable and DescribeTable return
  `managed_versioning=false` (code: `result.setManagedVersioning(false)`, lines 321 and 371). The adapter returns no
  storage options: a search of the Java and Python adapters for `storage_options`, `credential` and `vend` finds
  nothing.

**The boundary.**

| Polaris controls | Polaris does not control |
| --- | --- |
| The name, in the same namespace as Iceberg tables | The table's commits ("no commit coordination") |
| The recorded location string, its allowed-location and overlap check | Its schema (not stored at all) |
| Who may create, load, list, drop the entry (RBAC or an external authorizer) | Its versions, tags, branches |
| An event per create / load / drop / list | Credentials: none are vended; the client brings its own |
| Maintenance policies can be attached to the hierarchy above it | Any cleanup of its files, on drop or otherwise |
| | Lineage of writes to it |

Inferred: because no credential is vended, Polaris's authorization on a Lance table gates the catalog entry only; a
principal with its own storage credentials reads and writes the dataset without Polaris seeing it.

---

## 2. Apache Gravitino

**What it is.** A "metadata lake": one server that federates many catalogs of many kinds (Hive, Iceberg, JDBC,
filesets, Kafka topics, models) under a metalake, with its own access control, tags, policies, statistics, a job
system, and auxiliary REST servers. One of those auxiliary servers speaks the Lance Namespace REST specification.

### 2.1 State and transactions

**How it is built.**
- One relational store. Doc, `docs/gravitino-server-config.md`: "`gravitino.entity.store` ... `relational` is the only
  supported value"; `JDBCBackend` over H2, MySQL or PostgreSQL.
- Code, `scripts/postgresql/schema-2.0.0-postgresql.sql`: about fifty tables, among them `metalake_meta`,
  `catalog_meta`, `schema_meta`, `table_meta`, `table_column_version_info`, `table_version_info`, `role_meta`,
  `role_meta_securable_object`, `owner_meta`, `tag_meta`, `policy_meta`, `job_run_meta`, `entity_change_log`,
  `scheduled_cron_tasks`, `table_maintenance_action`.
- Multi-table writes go through one database transaction (`SessionUtils.doMultipleWithCommit`, 62 call sites under
  `core/src/main/java`, for example `core/.../storage/relational/service/TableMetaService.java` lines 279 and 364).
- Deletes are soft at the row level and garbage-collected: `gravitino.entity.store.deleteAfterTimeMs` "How long in
  milliseconds deleted and superseded rows are kept", default 7 days. No API to restore a dropped object was found
  (search of `docs/` for "undrop", "undelete").
- Side effects outside the store are not atomic with it, and the code says so. `catalogs/catalog-lakehouse-generic/.../lance/LanceTableOperations.java`,
  in `dropTable`: "NOTE: the two steps below (metadata removal and dataset deletion) are NOT atomic. If the process
  crashes between them, the entity-store record will be gone but the Lance dataset will still exist on storage
  (orphaned data), or vice-versa. There is currently no recovery mechanism for this window."
- The pluggable table location provider states the same stance as a design rule (doc, `docs/lakehouse-generic-catalog.md`):
  "It is deliberately not a distributed transaction and not a recovery system ... a provider that manages storage has
  to reconcile against the catalog independently of these two calls."

**Conceptually strong.** A change log table (`entity_change_log`) that every replica polls is a simple, database-only
way to propagate invalidation.

**Weak or absent.** A relational database is mandatory. Metadata and storage can diverge with no reconciler.

### 2.2 Authorization

**How it is built** (doc, `docs/security/access-control.md` unless noted).
- Ownership plus privileges collected into roles, granted to users and groups. "Grants reach downward." Each
  privilege carries `ALLOW` or `DENY`, and "`DENY` always wins". Traversal privileges are required: `SELECT_TABLE` on
  a schema "on its own ... authorizes nothing" without `USE_CATALOG` and `USE_SCHEMA`.
- Off by default ("Authorization is off by default").
- The authorizer is pluggable by class name: `gravitino.authorization.impl`, default
  `org.apache.gravitino.server.authorization.jcasbin.JcasbinAuthorizer`; the only other shipped value is
  `PassThroughAuthorizer`, "not intended for production". Interface: `core/.../authorization/GravitinoAuthorizer.java`.
  No OPA or other external decision point was found in the repository.
- A different kind of external integration exists: **pushdown**. Doc, `docs/security/authorization-pushdown.md`:
  "Authorization pushdown applies a grant made in Gravitino to Apache Ranger, so the permission is enforced where the
  data is rather than only inside Gravitino." Configured per catalog (`authorization-provider=ranger` or `chain`).
  Gravitino writes the policy into Ranger; Ranger does not decide for Gravitino.
- Listing: "List operations normally do not fail; they return only the entries the caller is entitled to see."
- Consistency across replicas is eventual: "a revocation can take up to `jcasbin.changePollIntervalSecs` [default 3]
  to take effect across the cluster."

**Conceptually strong.** Explicit deny; filtered listings; grants written through to the system that guards the data.

**Weak or absent.** No external policy engine can decide. Pushdown covers Ranger's HadoopSQL and HDFS services only.

### 2.3 Storage access

**How it is built.** Doc, `docs/security/credential-vending.md`: vending is per catalog type. Scoped token providers
(`s3-token`, `gcs-token`, `adls-token`, ...) "scope credentials to a path, so only Fileset and Iceberg catalogs
support them. Other catalogs vend static keys only". The `lakehouse-generic` catalog (the one Lance tables live in) is
not in the supported table. Location: derived from `location` properties on table, schema, catalog, or decided by a
pluggable `TableLocationProvider`; "the catalog owns neither [the path nor its content], storing the location string
verbatim". No overlap check was found in `catalogs/catalog-lakehouse-generic` (search for "overlap": no hit), and the
doc on purge says the catalog "does not verify storage ownership or provide isolation between tables".

**Weak or absent.** No overlap prevention for generic tables; no scoped credentials for them.

### 2.4 Events and audit

**How it is built** (doc, `docs/gravitino-server-config.md`).
- Event listeners are plugins. "Each operation emits up to three events: a pre-event before it runs, a post-event
  after it succeeds, and a failure event after it throws." "Throwing a `ForbiddenException` from a pre-event handler
  stops the operation before it runs, which makes pre-events a veto point". Dispatch modes `SYNC`, `ASYNC_SHARED`
  ("events can be dropped"), `ASYNC_ISOLATED`.
- Audit log: off by default; a formatter and a writer, default a Log4j file. "Every request that reaches the server
  produces at least one audit entry".
- Lineage (doc, `docs/lineage/gravitino-server-lineage.md`): "a pluggable lineage framework to receive, process, and
  sink OpenLineage events". The server is a **receiver and forwarder**: an HTTP source at `POST /api/lineage` takes
  OpenLineage run events that engines emit (Spark and Flink listener jars, `docs/lineage/lineage.md`), and sinks
  write them to a log file or to an OpenLineage HTTP endpoint such as Marquez. Back-pressure is a 429 at 90% of the
  queue. Gravitino does not derive lineage from commits.

**Conceptually strong.** Failure events and a synchronous veto point; lineage as a pass-through with back-pressure.

**Weak or absent.** No durable delivery. Lineage exists only if the engine's plugin emits it.

### 2.5 Background work and maintenance

**How it is built.** Unlike Polaris, Gravitino both stores policies and executes them, for Iceberg.
- Policies (doc, `docs/policies.md`): `system_iceberg_compaction`, `system_iceberg_rewrite_manifests`,
  `system_iceberg_snapshot_expiration`, `system_iceberg_orphan_file_removal` ("Consumer: Table maintenance service"),
  plus `system_row_filter`, `system_column_mask` (experimental), `system_access_control` ("Not enforced yet") and
  `custom`. A policy is associated with a tag, and the tag is assigned to objects.
- Execution (doc, `docs/table-maintenance-service/service.md`): "The table maintenance service runs inside the
  Gravitino server ... The service keeps the nearest policy of each built-in type and submits a Spark job." Triggers:
  a Quartz cron (`timeTrigger`) or `commitTrigger` ("A successful Iceberg REST table update enqueues one chained
  job"). Jobs run on Gravitino's job executor, `local` or `k8s` (`gravitino.job.executor`). State is in the entity
  store (`scheduled_cron_tasks`, `scheduled_commit_tasks`, `table_maintenance_action`).
- Scope (doc, `docs/table-maintenance-service/optimizer.md`): "What ships built in is deliberately narrower, and in
  alpha that includes Iceberg data file compaction on identity-partitioned tables and table-level orphan cleanup."
  Nothing for Lance.

**Conceptually strong.** A commit-triggered maintenance path, which only a catalog in the commit path can offer.

**Weak or absent.** Alpha, Iceberg only, Spark only; the catalog server becomes a scheduler with its own queue tables.

### 2.6 Tenancy and configuration as code

Hierarchy: metalake → catalog → schema → table (also fileset, topic, model). Helm charts under `dev/charts`:
`gravitino`, `gravitino-iceberg-rest-server`, `gravitino-lance-rest-server` (Deployment, ConfigMap, Service,
HTTPRoute/Ingress, PodDisruptionBudget, and for the main chart a schema-migrate Job). No operator, no CRD. Metalakes,
catalogs, roles, tags and policies are made through the API.

### 2.7 Operations

Doc, `docs/gravitino-server-config.md`, "Running More Than One Server": "Servers behind a load balancer share the
entity store but keep local caches. Each server polls the entity change log and invalidates entries that another
server has modified. The default poll interval is three seconds." Job staging files need shared storage
(`gravitino.job.stagingDir`).

### 2.8 What Gravitino does for a Lance table

**What it is.** Two pieces: a catalog provider `lakehouse-generic` that knows the Lance format (it links `lance-core`
and can create, open and delete a dataset), and a "Lance REST service" that exposes that catalog through a subset of
the Lance Namespace REST specification, either inside the Gravitino server ("auxiliary mode", port 9101) or as a
standalone process that calls Gravitino over HTTP.

**Which operations of the specification.** Doc, `docs/lance-rest-service.md`: "Index operations are not supported
yet." The table lists thirteen: CreateNamespace, ListNamespaces, DescribeNamespace, DropNamespace, NamespaceExists,
ListTables, CreateTable, DescribeTable, DropTable, TableExists, RegisterTable, DeregisterTable, DeclareTable. Code,
`lance/lance-rest-server/.../service/rest/LanceTableOperations.java` routes: `/describe`, `/create`, `/declare`,
`/register`, `/deregister`, `/exists`, `/drop`, `/drop_columns`, `/alter_columns` (the last two are in code and the
privilege table, not in the operations table). There is no route for insert, merge-insert, update, delete, query,
count, version listing or version creation, tags, branches, indexes or transactions. "Lance **branches and tags**
are not implemented; a request setting `branch` or `tag` is rejected with HTTP `400`." "Supports only **two levels
of namespaces** before tables."

**Where the metadata lives.** In Gravitino's relational entity store, as an ordinary table entity with columns
(`table_meta`, `table_column_version_info`), location in the table's `location` property. The schema is a copy
converted from Arrow to Gravitino types; it is refreshed from the dataset only under a mode
(`lance.schema-refresh-mode`, default `DECLARED_AND_EMPTY`: "never refreshes a table that is neither declared nor
empty, so such a table keeps its stored types even after the dataset version changes"). "Gravitino types cannot carry
Arrow field metadata."

**Does it take part in a commit.** No.
- `DescribeTable` and `DeclareTable` responses set `managed_versioning=false` (code,
  `lance/lance-common/.../ops/gravitino/GravitinoLanceTableOperations.java` lines 152 and 233).
- `CreateTable` writes an empty dataset and refuses rows: "A stream containing rows returns HTTP `406` before any
  metadata or dataset changes ... Write records through a Lance client or engine after creation." (Code: `Dataset.write()
  ... .mode(WriteParams.WriteMode.CREATE)` in `LanceTableOperations.java` line 506.)
- "`DescribeTable` currently returns the latest metadata even when a historical `version` is requested; versioned
  metadata reads are not implemented."
- Doc: "Authorization governs metadata requests; engines access Lance data files directly."

**How credentials reach the client.** Not vended. Doc, same file: "Responses can include configured storage
credentials: namespace descriptions resolve secret properties, and table descriptions, creation and declaration
responses return effective `storage_options` from catalog defaults and table overrides. These are shared configured
credentials, not temporary credentials restricted to the caller's table privileges. ... Per-user, scoped credential
vending is not implemented."

**Governance that applies.**
- Authorization: only in auxiliary mode. A privilege table maps each Lance operation to Gravitino privileges
  (`DescribeTable` needs `USE_CATALOG`, `USE_SCHEMA` and `SELECT_TABLE` or `MODIFY_TABLE`, or ownership; `DropTable`
  and `DeregisterTable` need ownership). Listings are filtered: "Filtering happens before pagination; hidden entries
  do not consume page slots." In standalone mode: "Standalone per-user authorization and scoped temporary credential
  vending are outside the supported scope", and "**Standalone mode is not recommended**".
- Audit and events: the Lance REST requests pass through Gravitino's event and audit pipeline (the doc configures
  `trustedProxies` "for audit client addresses").
- Lineage: Gravitino's lineage is fed by engine plugins; the Lance pages do not mention lineage, and since Gravitino
  is not in the write path it sees no Lance write. (Inferred from the two facts above.)
- Drop safety: "The generic catalog rejects purging external or registered Lance tables by default ... Tables
  created, declared or registered through Lance REST are external". An administrator can allow it per catalog with
  `lance.allow-external-table-purge=true`, which "permits deletion at caller-supplied locations; it does not verify
  storage ownership or provide isolation between tables."
- Maintenance: none for Lance.

**The boundary.**

| Gravitino controls | Gravitino does not control |
| --- | --- |
| The name, under metalake → catalog → schema | The table's commits (`managed_versioning=false`; no data-plane or version routes) |
| The recorded location (default derived, or a pluggable provider) | Its versions, tags, branches (rejected) |
| A converted copy of the schema, possibly stale | The authoritative schema (the dataset's) |
| Who may see, create, alter, drop the entry; filtered listings | Who can read the files: credentials returned are shared static ones |
| Creating an empty dataset; deleting a dataset on drop where allowed | Overlap between two tables' locations |
| Audit entries and events for the metadata calls | Compaction, version cleanup, indexing |
| Tags and policies on the entry | Lineage of writes |

---

## 3. Unity Catalog (open source)

**What it is.** A catalog server for tables (Delta first, also Iceberg through an Iceberg REST endpoint), volumes,
functions and ML models, with a three-level name (catalog.schema.table) under one metastore per server.

### 3.1 State and transactions

- One relational database through Hibernate: H2 file, MySQL or PostgreSQL (`helm/README.md` line 13; `docs/server/deployment.md`).
  Repositories under `server/src/main/java/io/unitycatalog/server/persist/` (`TableRepository`, `OwnershipRepository`,
  `ExternalLocationRepository`, `CredentialRepository`, `DeltaCommitRepository`, `CleanupTaskRepository`,
  `StagingTableRepository`). Grants are rows in `casbin_rule` in the same database (`docs/server/auth.md`, "Native
  policy storage").
- Each repository operation runs in `TransactionManager.executeWithTransaction` (`persist/TableRepository.java`,
  about twenty call sites).
- The catalog holds commits only for **managed Delta** tables: `TableRepository.java` line 278, "Unbackfilled commits
  (managed Delta tables only; empty for external tables)". "Managed table creation is only supported for Delta and
  Iceberg formats." (line 910).
- Storage side effect on drop is a queued task in the same database, processed by `server/.../cleanup/StorageCleanupWorker.java`
  ("Polls and processes cleanup tasks on one scheduled thread", with a lease duration, attempt timeout and retry
  backoff). A location with a pending cleanup is refused for reuse (`persist/utils/ExternalLocationUtils.java` line
  149, "Input path overlaps pending storage cleanup").

**Conceptually strong.** The cleanup task is a database row with a lease, so the drop and the intent to clean are in
one store and any replica can pick the work up. Pending cleanups block reuse of the path.

**Weak or absent.** A relational database is mandatory. Whether the task row is written in the same transaction as
the drop was not confirmed.

### 3.2 Authorization

Doc, `docs/server/auth.md`.
- Privileges on a hierarchy (metastore, catalog, schema, table, volume, function, model, external location, storage
  credential), with usage privileges required on parents and ownership as an alternative. Endpoints carry an
  authorization expression (code: `server/.../auth/annotation/AuthorizeExpression.java`, `AuthorizeExpressions.java`).
- Two backends: Casbin (default) and **OPA** (`server.authorization.provider=opa`, code `auth/OpaAuthorizer.java`).
  "UC keeps its existing authorization expressions and asks OPA about each evaluated privilege, including `OWNER`."
  "Each privilege is a separate request." "Transport errors, timeouts, HTTP errors, and missing or invalid decisions
  fail the UC request with HTTP 503. UC never falls back to native grants." "Path resolution, overlap validation, and
  staging-table creator checks remain in UC."
- Listing: "For both Casbin and OPA, permission denials and client errors (HTTP 4xx) filter out individual items."
  "A large list can still require calls for every item."
- Replicas: with Casbin the policy is in memory per process and refresh is "**off by default**"; "The poll interval
  is not a guaranteed revocation deadline."

**Conceptually strong.** The external engine answers one small question (principal, privilege, resource with
parents); the catalog keeps the expression logic. Fail-closed on engine failure.

**Weak or absent.** No groups or roles are sent ("UC has no group or role membership to include"). No batch call to
OPA: one HTTP call per privilege per list item.

### 3.3 Storage access

- Temporary credentials per table, path, volume or model version (`service/TemporaryTableCredentialsService.java`,
  guarded by `AuthorizeExpressions.VEND_TABLE_CREDENTIAL`; `READ` maps to `SELECT`, `READ_WRITE` to `SELECT` and
  `UPDATE`). AWS: an STS session with a generated policy (`service/credential/aws/AwsCredentialGenerator.java` lines
  164-172 call `AwsPolicyGenerator.generatePolicy(privileges, locations, ...)` and pass `.policy(awsPolicy)`).
- External locations and storage credentials are first-class objects with their own privileges (`CREATE EXTERNAL
  TABLE`, `READ FILES`, `WRITE FILES` on an external location).
- Overlap: an external table's location must not be a bucket root and must not overlap managed storage
  (`persist/TableRepository.java` lines 895-901, `validateNotOverlapWithManagedStorage`). External locations must not
  overlap each other (`ExternalLocationUtils.java` line 48: "Unity Catalog requires external locations to have
  non-overlapping URL hierarchies"). A check that two external **tables** do not overlap each other at create was
  not found in `TableRepository`.

### 3.4 Events and audit

Doc, `docs/server/configuration.md`, "Audit records": off by default; a sink class (`FileAuditSink`) receives one JSON
line for "each storage credential it issues, each permission change, and each storage cleanup it queues". Actions:
`ISSUE_CREDENTIAL`, `GRANT`, `REVOKE`, `CLEAR_RESOURCE_GRANTS`, `CLEAR_PRINCIPAL_GRANTS`, `QUEUE_CLEANUP`. "Audit
records are best effort: one can be lost when the sink fails". No event stream (Kafka, webhooks) and no lineage were
found in `docs/server`.

**Conceptually strong.** The audit trail is centred on the credential issue, which is the one moment the catalog
sees data access.

**Weak or absent.** No metadata-change events, no lineage, best-effort audit.

### 3.5 Background work and maintenance

Only the storage cleanup worker after a drop of a managed object. No compaction, no retention, no maintenance policy
store. No undrop found.

### 3.6 Tenancy and configuration as code

Metastore (one per server) → catalog → schema → table / volume / function / model. Helm chart `helm/` (server and UI
templates, HTTPRoute or Ingress); no operator, no CRD. Catalogs, grants, external locations and credentials are made
through the API or CLI.

### 3.7 Operations

Shared between replicas: the database, the token signing key (a Secret the chart keeps on uninstall, `helm/README.md`
line 33), the key that encrypts stored credential secrets, and the same authorization settings on every instance.
Casbin policy refresh must be turned on for more than one instance.

### 3.8 What Unity Catalog does for a Lance table

**How it is built.**
- Unity has no Lance format. `api/all.yaml`, `DataSourceFormat`: `DELTA, ICEBERG, CSV, JSON, AVRO, PARQUET, ORC,
  TEXT`. A table in a format it does not manage is an `EXTERNAL` table: a name, a `storage_location`, a declared
  column list, properties.
- The Lance side (`lance-namespace-impls`, `docs/src/unity.md`, `java/lance-namespace-unity/.../UnityNamespace.java`):
  a client-side adapter. A Lance table is a Unity table with `table_type` `EXTERNAL` and property `table_type=lance`.
  "Unity Catalog does not natively recognize the `LANCE` data source format, so `data_source_format` is set to
  `TEXT`". Operations: CreateNamespace, ListNamespaces, DescribeNamespace, DropNamespace (restrict only),
  DeclareTable, ListTables (filtering on the property), DescribeTable, DeregisterTable, and the exists calls.
  `managed_versioning=false` (code lines 365 and 409). The adapter does not call Unity's temporary-credentials API:
  no `storage_options`, `credential` or `vend` in `UnityNamespace.java` or `python/.../unity.py`.

**The boundary.**

| Unity controls | Unity does not control |
| --- | --- |
| The name, catalog.schema.table | The table's commits (commit coordination is for managed Delta only) |
| The recorded location; no overlap with managed storage; external-location privileges | The format: it believes the table is `TEXT` |
| Who may see, create, drop the entry; filtered listings; OPA can decide | The schema: a declared column list, not read from the dataset |
| Unity can issue a scoped temporary credential for an external table's location, and audits it | Whether the Lance client uses it: the adapter does not ask for one |
| | Versions, cleanup (external tables are not cleaned), maintenance, lineage |

---

## 4. The comparison in one table: what "governs a Lance table" means in each

| | Polaris | Gravitino | Unity Catalog |
| --- | --- | --- | --- |
| Where Lance support lives | Client adapter in the Lance project, over Polaris's generic-table API | In Gravitino: a Lance-aware catalog provider and a Lance Namespace REST server | Client adapter in the Lance project, over external tables |
| Named as supported by the catalog's own docs | No (blog post only) | Yes ("Gravitino fully supports the **Lance** lakehouse format") | No |
| Catalog stores | name, format, location, properties | name, location, properties, a converted schema copy | name, location, declared columns, properties (format recorded as `TEXT`) |
| Takes part in a commit | No | No | No |
| `managed_versioning` reported to the Lance client | false | false | false |
| Lance Namespace operations | namespace CRUD, declare, describe, list, deregister | those plus create (empty), register, drop, alter/drop columns | namespace CRUD, declare, describe, list, deregister |
| Credentials for the table's files | none | shared static storage options from catalog properties | available from Unity, not used by the adapter |
| Location overlap refused | yes, by default (slow path for generic tables) | no | against managed storage only (as read) |
| Authorization on the entry | RBAC or external (OPA preview, Ranger) | built-in, auxiliary mode only; filtered listing | Casbin or OPA; filtered listing |
| Events / audit for the entry | best-effort events | events with veto; file audit | credential and grant audit only |
| Lineage of writes | none | only what an engine plugin sends | none |
| Maintenance for Lance | none (policy store is Iceberg-typed) | none (service is Iceberg-only) | none |
| Drop deletes data | never (drop without purge) | refused for external tables unless a catalog flag allows it | no (external) |

In all three, "governing a Lance table" means governing a **registry entry**: a name bound to a location, with access
control on the entry. None of them sees, judges, records or coordinates a Lance commit; none knows the table's
versions; none cleans or maintains it; none produces lineage for it; and none hands a Lance client a credential scoped
to that table.

---

## 5. Where the three agree

Each row is a design choice all three share, with the source in each.

1. **All catalog and governance state is in one transactional database, and the catalog is otherwise stateless.**
   Polaris: `metastores/_index.md` (PostgreSQL "Recommended for production"). Gravitino: `gravitino-server-config.md`
   ("`relational` is the only supported value"). Unity: `helm/README.md`, `persist/`. None keeps governance records
   on object storage.
2. **Grants live in the same store as the entities.** Polaris `grant_records` in `schema.sql`; Gravitino
   `role_meta_securable_object`, `owner_meta`; Unity `casbin_rule` (`auth.md`).
3. **Effects on object storage are not atomic with the metadata write, and are handled by a queued task or not at
   all.** Polaris: task entity, with a TODO admitting the gap (`AtomicOperationMetaStoreManager.java` line 1262).
   Gravitino: "NOT atomic ... no recovery mechanism" (`LanceTableOperations.java`). Unity: cleanup task rows and a
   leased worker (`cleanup/StorageCleanupWorker.java`).
4. **Events and audit are best-effort, asynchronous, and separate from the transaction.** Polaris `events.md`
   ("asynchronous and best-effort"); Gravitino `gravitino-server-config.md` (`ASYNC_SHARED`: "events can be
   dropped"); Unity `configuration.md` ("Audit records are best effort"). None has an outbox or an at-least-once
   guarantee.
5. **Authorization is checked in the catalog at the request, on a privilege hierarchy with inheritance downward.**
   Polaris `access-control.md`; Gravitino `security/access-control.md` ("Grants reach downward"); Unity `auth.md`
   ("Catalog and schema grants still apply to their matching child objects").
6. **Scoped, short-lived credentials are derived from the table's location and the caller's privilege, for the
   formats the catalog manages.** Polaris `vended-credentials.md`; Gravitino `security/credential-vending.md` (token
   providers for Fileset and Iceberg); Unity `AwsCredentialGenerator.java`. A revoked grant stops the next vend, not
   a credential already issued (Polaris states this; the others were not checked).
7. **Compaction and retention are not run by the catalog's request path.** Polaris stores policies and runs nothing.
   Unity has neither. Gravitino submits Spark jobs to an executor; the work itself still runs outside the server.
8. **Deployment is a Helm chart of stateless replicas; no operator, no CRDs; tenants, catalogs, roles and grants are
   created through the API, not declared.** Polaris `helm/polaris/templates`; Gravitino `dev/charts`; Unity `helm/`.
9. **For a Lance table, the catalog is a registry and takes no part in commits** (section 4).

### Where they do not agree

- **External policy decision.** Polaris: an SPI with OPA (preview) and Ranger, with batch decisions. Unity: OPA per
  privilege, fail-closed. Gravitino: no external decision point; it pushes grants out to Ranger instead.
- **Listing.** Gravitino and Unity filter listings by default. Polaris authorizes the container and filters entities
  only behind a flag, and not for generic tables.
- **Who executes maintenance.** Polaris: engines, from stored policies. Gravitino: its own in-server scheduler
  submits jobs (alpha, Iceberg). Unity: nobody.
- **Deny rules.** Only Gravitino has explicit `DENY`.
- **Location overlap.** Polaris refuses by default; Unity refuses against managed storage and between external
  locations; Gravitino's generic catalog does not check.
- **Tenancy above the catalog.** Polaris: realm (in every key, from a request header). Gravitino: metalake. Unity:
  one metastore per server.
- **Lineage.** Gravitino forwards OpenLineage events it receives. Polaris has stubs. Unity has none.
- **Replica consistency of grants.** Gravitino polls a change log every 3 s by default. Unity's Casbin refresh is off
  by default and "not a guaranteed revocation deadline". Polaris was not checked on this point.
- **Soft delete.** Gravitino keeps deleted rows for 7 days for garbage collection but exposes no restore. Polaris
  and Unity have none.

---

## Could not confirm

- **Published documentation sites** (polaris.apache.org, gravitino.apache.org, docs.unitycatalog.io) were not
  fetched; the repositories' docs trees on the default branch were read instead. A released version may differ from
  the unreleased docs quoted here.
- **Polaris**: the OPA and Ranger extensions' source was not read (docs and the SPI only). How entity caches and
  grant changes propagate between replicas was not read. The NoSQL backends' atomicity was not read. The catalog's
  allowed-locations code path was read in docs only. That generic tables vend no credential rests on the doc
  statement and on the handler having no credential method; the handler does hold a `credentialManager()`, used for
  federated catalogs.
- **Gravitino**: `JcasbinAuthorizer`'s internals were not read. That no restore API exists rests on a text search of
  `docs/`. That Lance REST calls reach the audit log and event listeners rests on the docs' configuration text, not
  on a traced call. The lineage statement for Lance is an inference. The relation between Lance REST "create" and the
  privilege-table entries `AlterColumns` / `DropColumns` (present in code and the privilege table, absent from the
  operations table) was not resolved.
- **Unity Catalog**: whether the cleanup task row is written in the drop's transaction; whether two external tables
  may overlap (only the callers of `ExternalLocationUtils` in `TableRepository` were read, and only the
  managed-storage check appears at create); the absence of an event stream and of lineage rests on a search of
  `docs/server`. The AWS session policy's exact statements were not read, only that one is generated from privileges
  and locations.
- **Lance Namespace integrations**: `lance-namespace-impls` is a separate repository from `lance-format/lance-namespace`
  (whose README points to it). Its last commit is 2026-10-10; whether the published adapters match was not checked.
  Gravitino's own doc page there (`docs/src/gravitino.md`) says native Lance REST support began in Gravitino 1.1.0.
