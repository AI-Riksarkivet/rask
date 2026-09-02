# Build or buy the catalog: rask's own vs Lakekeeper, Gravitino, Unity Catalog, DuckLake

Written 2026-09-02. Locked by the owner: **the Lance table format** (no Iceberg, Delta or Parquet tables, ever), **OpenFGA** for authorization, **the OpenLineage spec** for lineage. The question is whether any of the four external catalogs should replace, or sit under, the catalog rask has built.

Sources: the Lakekeeper source at `0.13.1` (2026-09-01), the `lance-namespace-impls` repository (2026-07-24, the official implementation specs for Unity, Polaris, Iceberg REST, Glue, Hive and the Gravitino pointer), the vendored Lance Namespace spec, rask at `feec956`, and three research passes on Gravitino, Unity OSS and DuckLake **(pending; sections marked)**.

---

## 1. The verdict in advance

**Keep rask's catalog as the Lance-aware layer. Nothing on the list can replace it under the format lock.** Unity, Polaris and Lakekeeper give a Lance table exactly eight metadata operations and `managed_versioning=false`: a pointer with a marker property, no commit coordination, no versions, no tags, no branches, no blobs. Gravitino is the only one that speaks the Lance Namespace REST shape natively, and it serves 15 of the 54 operations with `managed_versioning` hard-coded to `false`: the same registry floor behind a spec-shaped facade. DuckLake is disqualified on the format lock alone (its data files are Parquet by specification, hard-coded in the implementation). The honest question is therefore not "which catalog replaces ours" but "**should one of them be the registry and authz substrate under ours**", and the answer turns on OpenFGA: Lakekeeper is the only one that already runs the authz store you have locked, and it does so with a model close enough to rask's that adopting it would mean discarding rask's, not merging.

What the DIY path must then earn, in exchange for owning the catalog: the operational backlog the earlier analysis found (conditional writes, leases, task records, storage profiles, a proven purge). Those are the exact things Lakekeeper ships. That is the trade.

---

## 2. What the lock does to the comparison

A catalog earns its keep on three things: identity and governance of objects, coordination of commits, and the operational machinery around both (tasks, retention, tenancy, HA). Under a **closed, single format** the middle one changes character. An Iceberg catalog coordinates commits because Iceberg puts the commit pointer in the catalog. Lance puts the commit in the object store (put-if-not-exists on the manifest) and offers the catalog a *choice*: stay out of the commit path, or become an external manifest store through `CreateTableVersion`. Only a Lance-aware catalog can take the second option. A format-agnostic catalog can only take the first, and then everything that makes rask a lakehouse rather than a file registry (versions, tags as publication, branches, stable row ids, blob v2 tiers, the quality gate at the commit, provenance in the row) has to live somewhere else anyway.

So the comparison collapses to two rows for every candidate:

- **What it does for a Lance table.** Measured against the 54-op Lance Namespace spec and the external-manifest-store protocol.
- **What it does for everything else.** Registry, hierarchy, authz, events, tasks, tenancy, HA, UI.

And two locked constraints filter it: OpenFGA (does the candidate use it, wrap it, or fight it) and OpenLineage (does the candidate have lineage at all; none of the four is a lineage store, so this is always DIY).

---

## 3. What each foreign catalog actually gives a Lance table

From the official implementation specs in `lance-namespace-impls/docs/src/*.md`, which are the contracts the Lance client library uses.

| catalog | how a Lance table is represented | operations fulfilled | `managed_versioning` | commit coordination |
| --- | --- | --- | --- | --- |
| Unity Catalog | an `EXTERNAL` table with `data_source_format=TEXT` (Unity does not recognise Lance) and a property `table_type=lance` (`unity.md:39-51`) | the basic eight: create/list/describe/drop namespace, declare/list/describe/deregister table (`unity.md:53-195`) | `false` (`unity.md:138, 168`) | none |
| Apache Polaris | a Generic Table with `format=lance` (`polaris.md:39-51`) | the basic eight (`polaris.md:53-201`) | `false` (`polaris.md:143, 173`) | none |
| Iceberg REST catalog | **a companion Iceberg table with a dummy schema** at the same location, marked `table_type=lance` (`iceberg.md:7, 51-53`) | the basic eight (`iceberg.md:55-213`) | `false` (`iceberg.md:149, 181`) | none |
| Lakekeeper | a Generic Table with `format=lance` via its own `/lakekeeper/v1/{prefix}/namespaces/{ns}/generic-tables` API (`docs/generic-tables.md`), which is **not** the `/polaris/v1/...` path the Lance Polaris client calls (`polaris.py:266-456`), so Lance reaches Lakekeeper today through the Iceberg-REST dummy-table wrapper or a new client impl | identity, governance, vending, remote signing, soft-delete/undrop, protection, rename, listing, 16 per-action permissions; **"Commit coordination: no. Schema enforcement: no"** (`generic-tables.md`, Capabilities table) | not part of the API | none, by design: "engines write directly" |
| Gravitino ≥ 1.1.0 | a table in a multi-format `lakehouse-generic` catalog (`format=lance`), exposed through a native Lance REST server on port 9101 (`docs/lance-rest-service.md`) | **15 of 54**: the basic eight plus Create, Register, Exists and two alter-column ops; no versions, tags, branches, indices, transactions or data ops (`LanceTableOperations.java:74-304`) | **`false`, hard-coded** (`GravitinoLanceTableOperations.java:151`) | none: returns location and static storage keys; writers commit to storage |
| DuckLake | not representable: `ducklake_data_file.file_format` is "Currently, only `parquet`" by specification and hard-coded in the implementation | n/a | n/a | n/a |

The pattern is exact. Every catalog that predates the Lance spec, including the two that are Iceberg REST servers, offers Lance the eight-op "basic implementation" the spec itself recommends as a floor (`namespace.md:1884-1946`, "Recommended Basic Operations"). That floor is a registry. It is what the spec designed so that "as many catalogs as possible" can hold a Lance pointer; it is not a lakehouse.

rask's catalog, by contrast, routes 54 of 54 and backs 47 (`.claude/skills/rask-lance-catalog/SKILL.md:18-23`), serves the Arrow-IPC data plane in-process, coordinates commits with a Lance conflict taxonomy, and holds versions, tags, branches and indices. The previous analysis found its one structural error on this axis: the governance sits on a non-spec `/commit` route instead of the spec's `CreateTableVersion`. That is a relocation, not a reason to buy.

---

## 4. Lakekeeper, in full

**What it is.** A Rust Iceberg REST catalog at 0.13.1: Postgres 15+ as the only backend, OpenFGA as the default authorizer with a versioned model (v2.1 → v4.10), Vault KV2 or Postgres for secrets, NATS or Kafka CloudEvents, credential vending and remote signing per warehouse storage profile, soft-delete with expiration and purge on a `task` queue (`FOR UPDATE SKIP LOCKED`, heartbeats, `max_retries`), an `idempotency_record` written inside the mutation transaction, trigger-incremented `version` columns, a UI, admission gates, contract-verification hooks, endpoint statistics. **No lineage** (zero Rust files mention it; `customize.md:52` positions event listeners as the way to "feed lineage tools").

**What it does for Lance.** Generic tables (§3). The catalog "does not commit format-specific metadata for generic tables — readers and writers go directly to the storage location after obtaining catalog metadata and credentials." The doc is candid: "Format libraries that talk to S3 through their own client (Lance, for example) generally expect static credentials and do not implement the Iceberg signer protocol, so for those, prefer a storage that supports STS-vended credentials."

**The OpenFGA overlap, precisely.** Lakekeeper's model has twelve types (`server, project, warehouse, namespace, lakekeeper_table, lakekeeper_view, lakekeeper_generic_table, lakekeeper_catalog_tag, model_version, role, user, auth_model_id`). rask's has ten (`user, team, role, project, warehouse, namespace, table, materialized_view, transaction, annotation_project`; `model.fga:41-444`). The generic-table relations are a near-isomorph of rask's table relations:

| Lakekeeper `lakekeeper_generic_table` | rask `table` |
| --- | --- |
| `ownership`, `pass_grants`, `manage_grants` with `managed_access_inheritance from parent` | `owner`, `pass_grants`, `manage_grants` with the same `but not managed_access_inheritance from parent` clause |
| `describe`, `select`, `modify` rungs | `reader`, `writer` rungs |
| `can_read_data: select`, `can_write_data: modify`, `can_get_metadata: describe` | identical names |
| `can_grant_<rung>: manage_grants or (<rung> and pass_grants)` | identical shape |
| `can_undrop`, `can_set_protection`, `can_get_tasks`, `can_control_tasks`, `can_manage_tags` | absent |
| absent | `validator` rung and `can_promote`; `non_expired_grant` conditions (time-boxed grants); `can_be_notified`; `can_create_branch/tag`, `can_update_tag`, `can_restore` |

Same lineage of design (rask's model cites Lakekeeper's `managed_access` pattern by construction). The differences are the product: rask's `validator`/`can_promote` is the medallion's approval rung; its conditional grants are time-boxed; its `can_be_notified` drives the inbox; its branch/tag/restore rungs exist because the catalog is Lance-aware. Lakekeeper's extras are task control and protection, which rask lacks and should have. **You cannot run both models on one store.** Adopting Lakekeeper's authz means either re-expressing rask's rungs as Lakekeeper extensions (it supports a custom `Authorizer` trait, in Rust) or running two OpenFGA stores with two sources of truth for who may write a table.

**What it would cost to put Lakekeeper under rask.**

- Two hierarchies: Lakekeeper's `project > warehouse > namespace > generic-table` in Postgres, and rask's `_projects/`, `_warehouses/`, `__manifest` on the object store. One must become derived from the other; the derived one is a cache with all the drift the reconciler already chases.
- Two authz models on one OpenFGA (see above), or rask's model deleted.
- A Lance-aware layer still has to exist for everything past the eight ops. That layer *is* rask's catalog minus its registry. So the deletion is `projects.py`, `warehouses.py`, part of `fga_deps.py`, and the trash/protection/policy records; the retention is `dataplane.py`, `publication.py`, `vending.py` (or Lakekeeper's), tags/branches/versions/indices/data endpoints, lineage emission, the quality gate.
- Rust, Postgres 15+, and a second control plane in the chart. rask's owner ruling that "the estate needs no relational DB" for the catalog would be reversed for the registry.
- The Lance client would reach Lakekeeper through the Iceberg-REST dummy-table wrapper (the Polaris path does not match Lakekeeper's route prefix), which means every Lance table also exists as a fake Iceberg table in the catalog, or a new client implementation is written and maintained.

**What it would buy.** Every item in cluster 2 and cluster 3 of the earlier gap analysis: the task queue with leases and attempts, a proven purge, idempotency records, per-warehouse storage profiles with validation and overlap checks, a short-term-credential cache, HA with no local state, admission gates, endpoint statistics, a UI. Those are real, and they are the things rask's backlog is largest on.

**Verdict on Lakekeeper.** Not as a replacement: it holds a Lance table as an opaque pointer by design. As a substrate: possible, and the only candidate whose authz store is the one you locked, but it costs a second hierarchy, a second authz model or the loss of rask's, a Postgres, and a Rust codebase to extend, in exchange for operational machinery rask can build from primitives it already has (`records.py` conditional writes, JetStream work queues, tag-as-lease). The better use of Lakekeeper is as a **design reference**: copy `task`/`task_log`/`idempotency_record`/`version`-trigger shapes onto object-store records and JetStream, and copy the `can_undrop`/`can_set_protection`/`can_control_tasks` rungs into `model.fga`.

---

## 5. Apache Gravitino

The candidate that could have changed the question, and does not. Verified against a shallow clone at `d0c656a` (main, `2.0.0-SNAPSHOT`; releases 1.1.0 2025-12-16, 1.2.0 2026-03-13, 1.3.0 2026-06-29) since `gravitino.apache.org` is egress-blocked here.

**What it is.** A federated "metadata lake": Java 17, Jetty and Jersey, a relational entity store only (H2 for dev, MySQL, PostgreSQL 12–16; 42 tables), a main server on 8090 plus optional in-process or standalone auxiliary services (Iceberg REST on 9001, **Lance REST on 9101**), three Helm charts, HA as N stateless servers polling an `entity_change_log` table every 3 s. Hierarchy `metalake > catalog > schema > {table, view, fileset, topic, model, function}`.

**What its Lance REST service actually serves.** The route list in `LanceNamespaceOperations.java:55-195` and `LanceTableOperations.java:74-304`, diffed against the 54 operation ids in the vendored `spec.yaml`:

| served (15) | not served (39) |
| --- | --- |
| Create/List/Describe/Drop/Exists namespace; List tables; Create, Declare, Register, Deregister, Describe, Exists, Drop table; AlterTableDropColumns; AlterTableAlterColumns | **every version op** (Create/BatchCreate/BatchDelete/List/Describe table versions, RestoreTable), **BatchCommitTables**, **every tag, branch and index op**, **every Arrow-IPC data op** (Insert, MergeInsert, Update, Delete, Query, CountRows, Explain, Analyze), transactions, RenameTable, AddColumns, BackfillColumns, field and schema metadata, materialized views, ListAllTables |

- `DescribeTable` **hard-codes `managed_versioning=false`** and ignores the `version` parameter ("versioned describe is not implemented; returning latest", `GravitinoLanceTableOperations.java:114-120, 151`). It cannot be an external manifest store.
- It does not coordinate commits: it returns a location and merged `storage_options`; writers commit to storage with Lance's own CAS. The stored `lance.version` is a "checked at this version" marker, stale unless `VERSION_CHECK` reopens the dataset on every load.
- Namespaces are limited to two levels ("Tables cannot be nested deeper than schema level"); rask's `project > warehouse > namespace > table` would have to flatten. 1.3.0's hierarchical namespaces landed in core and Iceberg REST, not the Lance server.
- The backing catalog is `lakehouse-generic`, deliberately multi-format: a `DeltaTableDelegator` ships beside the Lance one. On a non-declared create it calls `Dataset.write(...).mode(CREATE)` itself and `OVERWRITE` "will delete the existing data directory first".
- Unknown routes fall to a JAX-RS 404 rather than the spec's problem+json `Unsupported`; index ops are "not supported yet"; 1.3.0 removed an endpoint and broke lance-spark 0.1.x; the docs warn to "test the exact connector versions".

So Gravitino is the eight-op floor plus create, register and two alter-column ops behind a spec-shaped REST facade. On the axis that matters under the lock it sits with Unity, Polaris and Lakekeeper: a registry.

**Authorization.** Its own RBAC (users, groups, roles, ~30 privileges, ownership cascade, DENY wins) enforced by jCasbin, with route guards as OGNL expressions; the Lance REST routes use the same mechanism. **Zero mentions of OpenFGA** in code, docs, design docs or issues. Two seams exist: a `GravitinoAuthorizer` interface to replace the decision engine, and a pushdown `AuthorizationPlugin` (only Ranger and JDBC ship; no mapping for the Lance catalog). Either way roles, grants and owners stay stored and administered in Gravitino's tables, so OpenFGA would be a second source of truth or a mirror. Credential vending exists for the Iceberg path, but **the Lance path hands out static keys**: `DescribeTable` merges catalog and table `lance.storage.*` properties with stored secrets (`GravitinoLanceTableOperations.java:146-150`); no `CredentialProvider` is referenced under `lance/`.

**Lineage.** Gravitino **consumes** OpenLineage and does not store it: `POST /api/lineage` accepts RunEvents (openlineage-java 1.29.0) and forwards them to a log file or an HTTP sink such as Marquez. No GET, no lineage tables, no graph. The emission side is a separate Spark plugin. The AGE store and reconciler stay DIY, with Gravitino at best one more hop with a 429 back-pressure semantic.

**Events.** A rich `EventListenerPlugin` SPI (~339 event classes, pre-events can veto), but no external transport ships; driving a workflow engine means a Java plugin jar. Lance REST operations emit only the generic table events.

**What it would cost.** A JVM 17 plus a relational database beside the Python fleet; a second hierarchy (`metalake/catalog/schema/table`) and a second principal store (per-metalake users) kept consistent with rask's; a catalog that writes to and deletes from your buckets on create and overwrite unless every create is sent as `declare`; a format-agnostic seam that contradicts the Lance-only ruling rather than enforcing it; and everything past the fifteen ops still written and owned by rask, which is the entire lakehouse.

**Verdict on Gravitino.** Not a replacement, not a substrate. The one thing worth taking is its route and model handling as a conformance oracle for the fifteen ops it does serve, and the observation that an Apache project chose to expose Lance through the spec's REST shape, which validates rask's public-API direction.

---

## 6. Unity Catalog OSS

Verified against a shallow clone at `f12135f` (2026-09-02; latest release 0.6.0 on 2026-08-19, main is 0.7.0-SNAPSHOT) and the Lance Unity implementation. Everything here is the open-source `unitycatalog/unitycatalog`; Databricks' hosted product is a different thing and its lineage, tags and audit do not exist in OSS.

**What it is.** A JDK 17 Armeria server over Hibernate 6.5 (H2 by default; MySQL and PostgreSQL documented; schema by `hbm2ddl.auto=update`, no migration tool, "database schema upgrades" on the roadmap), jCasbin authorization, a React UI. An LF AI & Data **sandbox** project. HA is N replicas over one external database with an opt-in one-minute Casbin policy poll, or revocations stay invisible to other replicas until restart.

**What it does for a Lance table.** Its own docs steer Lance to **volumes**: governed pointers to directories with no schema and no versions. The Lance implementation instead registers an `EXTERNAL` table with `data_source_format=TEXT` because the format enum (`DELTA, ICEBERG, CSV, JSON, AVRO, PARQUET, ORC, TEXT`) has no Lance value, marks it `properties.table_type=lance`, freezes the Arrow schema into `columns` at declare time, and fulfils **10 of 54** operations (`UnityNamespace.java:107-427`): the eight basic ops plus `NamespaceExists` and `TableExists`, with `managed_versioning=false`. `/tables/{full_name}` has no PATCH, so properties, comment and columns are immutable after create. Commit coordination exists in UC only for managed Delta tables through its Delta API; for everything else it is never in the write path. The hierarchy is fixed at exactly `catalog.schema` (`CreateNamespace` rejects any other depth); there is no project or tenant level ("multi-tenancy" is roadmap v0.8+).

**Authorization.** jCasbin with a fixed 20-value privilege enum, grants through `/permissions`, hierarchy maintained by UC. There is a `UnityCatalogAuthorizer` interface, but its selection is **hard-coded** in `UnityCatalogServer.initializeAuthorizer` (`:173-196`): JCasbin when enabled, allow-all when disabled. Plugging OpenFGA means forking the server, and the interface's contract (UUID principal, UUID resource, UC's enum, UC's hierarchy) would still make UC's grant tables the thing the UI edits. Authentication is a single switch that couples authn and authz: UC exchanges an IdP token for **its own** access token, and the user must pre-exist in UC's local user table. Credential vending is real for AWS STS, ADLS and GCS, but **there is no S3 endpoint override**: S3-compatible stores such as RustFS are a roadmap item, so the one feature that would matter does not work against the estate's object store today.

**Lineage.** None. Zero hits for lineage or OpenLineage in server or API beyond view dependencies. The OpenLineage issue (#239, 2024-07) was closed the next day with no code; "Lineage" is a tentative v0.8+ roadmap entry.

**Events, soft-delete, tags, audit.** None of the four: no listener or webhook code ("change events" roadmap ❓), hard row deletes with no undrop, no tag entity, no audit log, no metrics or health endpoint.

**What it would cost.** A JDK 17 server, a third Postgres beside AGE and OpenFGA, a second hierarchy fixed at two levels under rask's four, a second authorizer with a second privilege vocabulary or a permanent fork, and every load-bearing capability still rask's: 44 of 54 operations, versions, tags, branches, indices, blobs, lineage, events, undrop, protection, and S3-compatible credentials. Its ecosystem (Spark, DuckDB, Trino and Daft connectors, the Delta API, the Iceberg REST endpoint, the MLflow registry backend) is Delta, Iceberg and MLflow-shaped and does not reach a table registered as `EXTERNAL/TEXT`; the Iceberg endpoint returns 404 for any table without an Iceberg metadata location.

**Verdict on Unity OSS.** The weakest fit of the four that can hold a Lance pointer at all: it substitutes for names, grants and IdP token exchange, and nothing else.

---

## 7. DuckLake

**Disqualified on the format lock alone, and independently on the other two.** Verified from the spec source (`duckdb/ducklake-web`) and a shallow clone of the extension (`357c385c`, 2026-09-01), since `ducklake.select` and `duckdb.org` are egress-blocked here.

- **The data-file format is closed to Parquet by specification.** `ducklake_data_file.file_format`: "Currently, only `parquet` is allowed." The FAQ: "The data files of DuckLake must be stored in Parquet." The reference implementation hard-codes it (`ducklake_insert.cpp:490, 542, 590`); the only non-Parquet enum is for deletion files (`puffin`); the orphan scan filters `.parquet` and `.puffin` only (`ducklake_metadata_manager.cpp:5163`). There is no format enum and no plugin seam. **You opened the Lance request yourself** (discussion #432, 2025-09-09) and it has zero maintainer replies a year later; the v1.1 and v2.0 roadmaps carry no file-format item.
- **No authorization.** "DuckLake relies on the authentication of the metadata catalog database." Roles are a v2.0 roadmap item. Nothing for OpenFGA to attach to except Postgres grants. No credential vending: every client holds bucket credentials plus database credentials.
- **No lineage.** Per-snapshot `author`, `commit_message`, `changes_made` and a CDC function. No cross-table or run-shaped model, no OpenLineage.
- **No REST catalog.** Every engine speaks SQL to the catalog database directly; the only server surface is the optional DuckDB-protocol `ducklake_commit`.

Maturity for the record: spec v1.0 on 2026-04-13, "production-ready" with backward-compatibility guarantees; DuckDB Labs; C++; engine support beyond DuckDB is alpha (DataFusion), write-only (Spark) or community (Trino, upstream issue open with no reply).

**Why it still matters: it is the purest statement of the design rask rejected.** The catalog database owns the snapshot pointer, the file list, the statistics and even small data (inlining); object storage holds only immutable files; a table is unreadable without the database ("Frozen DuckLake" ships the database file). Lance made the opposite bet: the commit is a put-if-not-exists in the object store, the catalog is optional, and an external manifest store "supplements but does not replace the manifests" so a reader unaware of it can still read the table (`file_format.md:5375-5381`). rask's "no relational catalog DB" ruling and its verified RustFS conditional-put support are downstream of that bet. Adopting DuckLake's shape would reintroduce the database the estate removed at P7a.

**What transfers as ideas, with the Lance equivalent:**

| DuckLake | Lance / rask equivalent |
| --- | --- |
| one snapshot row spanning N tables (multi-table ACID) | `BatchCommitTables`, atomic at the catalog layer only, and stubbed on rask's dir backend today; a true cross-dataset snapshot has no Lance analogue and would be a catalog-owned record |
| a global monotonic snapshot id with every row tagged `[begin, end)` | per-dataset versions plus tags and branches; an estate-wide point-in-time needs a catalog record mapping table → version |
| a changes log plus a typed conflict matrix re-checked on retry without rewriting data | Lance's per-dataset compatibility rules, already used by `_classify_commit_error`; DuckLake adds schema-object conflicts that rask handles with conditional JSON records |
| data inlining for small writes | none for rows; blob v2 placement solves small-*file* explosion inside the format, and compaction answers many small commits |
| per-file encryption key in the catalog row | none in the Lance format; out of scope under the lock |
| the 2026 server-side `ducklake_commit` from staged tables | **this is rask's `/commit` door**: client writes with vended creds, catalog folds metadata into a commit. DuckLake reached the same shape over the DuckDB wire; rask has it over HTTP with FGA in the path |
| `require_commit_message` and author per snapshot | a provenance floor rask already exceeds with OpenLineage |

---

## 8. The DIY case, stated against the alternatives

**Why build.**

1. **The format lock makes Lance-awareness the whole value.** Every capability that distinguishes a lakehouse from a file registry (versions, tags as publication, branches, stable row ids, blob v2 placement and external descriptors, the commit conflict taxonomy, provenance in the row, the quality gate at the commit, format-aware maintenance refusals) requires a catalog that opens Lance datasets. None of the four does, with Gravitino pending. That layer must be written and maintained by you regardless of what sits under it.
2. **OpenFGA is locked, and rask's model is the product.** The `validator`/`can_promote` rung, time-boxed grants, `can_be_notified`, branch/tag/restore rungs: none exists in any candidate. Lakekeeper's model is the closest and adopting it discards those.
3. **OpenLineage is locked, and no candidate is a lineage store.** The AGE graph, the storage-to-graph reconciler that back-fills from Lance manifests, column lineage, provenance in the row: DIY under every option.
4. **No relational catalog DB is an owner ruling with a technical basis.** Lance puts the CAS in the object store, so the catalog needs no transaction log. Every candidate reintroduces Postgres (or, for DuckLake, is Postgres) as the catalog's source of truth.
5. **The spec-verbatim goal is reachable from where rask is.** 54 of 54 routed, 47 backed, and the governance relocation onto `CreateTableVersion` is catalog code, not infrastructure.

**Why buying is tempting, honestly.**

1. The operational backlog is real and it is exactly what Lakekeeper ships: leases, attempts, idempotency, storage profiles, a proven purge, HA, a UI.
2. A community maintains the registry, the FGA model versioning, the vending across three clouds, and the Iceberg ecosystem, none of which rask wants to own.
3. If Gravitino's Lance REST is complete, the spec-conformance work is already done by someone else.

**Why the temptation does not survive the locks.** The things you would buy are the registry and the operations around it. The things you cannot buy are the Lance-aware layer, the FGA model, and the lineage store. The bought registry would sit under a layer you still own, duplicate its hierarchy, and split its authz. The operational machinery is buildable on primitives already in the tree (`records.py` conditional writes proven on RustFS, JetStream work queues already used by ingest, tags as GC leases), and the earlier analysis lists the eight items in order.

---

## 9. What to take from each, without adopting it

| from | take |
| --- | --- |
| Lakekeeper | the `task`/`task_log`/`task_config` shape (one live row per entity and queue, attempt counter, heartbeat, `max_retries`) as JetStream work queues or object-store records; `idempotency_record` written after the mutation as a conditional control-root object; trigger-style `version` on registry records (rask's etag already is this); `can_undrop`/`can_set_protection`/`can_get_tasks`/`can_control_tasks`/`can_manage_tags` rungs; per-warehouse storage profile + secret ref + validation probe ladder; the STC cache with single-flight and half-lifetime expiry; the admission-gate seam; instance admins and the bootstrap latch; the OpenFGA model-versioning object |
| Gravitino | its Lance REST route and model handling as a conformance oracle for the fifteen ops it serves; the fact that an Apache project exposes Lance through the spec's REST shape, which validates rask's public-API direction; its 339-class pre/post/failure event vocabulary as a naming reference for rask's control lane |
| Unity OSS | the volume concept as a governed container for non-table files, if rask ever needs one beside blob v2 external references; the model-version registry shape with `finalize` and per-version vended credentials, as a comparison for rask's Lance-dataset model registry |
| DuckLake | the multi-table transaction and snapshot-as-row semantics, mapped onto `BatchCommitTables` and a catalog-owned snapshot object; data inlining maps to blob v2 inline placement |
| the Lance spec's own catalogs | the eight-op floor as the minimum any external registry must serve, and the basis for rask's management API split |

---

## 10. The decision, as I would put it to the owner

Keep the catalog. Reposition it: the public surface is the Lance Namespace REST spec verbatim with managed versioning advertised, governance on `CreateTableVersion`, and everything rask-specific on a `/management` API in Lakekeeper's style. Take Lakekeeper's operational shapes as the design for the backlog and its FGA rungs as additions to `model.fga`. Gravitino, the only candidate that could have reopened this, serves 15 of 54 operations with managed versioning hard-coded off, static storage keys on describe, jCasbin authorization with no OpenFGA seam that keeps FGA authoritative, and a forward-only OpenLineage endpoint; it is a registry behind a spec-shaped facade, and the verdict stands.

**Re-evaluate only if** a candidate ships all three at once: managed versioning with `CreateTableVersion` and `BatchCommitTables` implemented, an authorizer that makes OpenFGA the source of truth for grants rather than a mirror, and a lineage store rather than a forwarder. None does today, and the gap between "registry" and "lakehouse" is exactly the code rask has already written.

---

## 11. The comparison on one page

| | rask (own) | Lakekeeper 0.13.1 | Gravitino 1.3.0 | Unity OSS 0.6.0 | DuckLake 1.0 |
| --- | --- | --- | --- | --- | --- |
| Lance Namespace ops served | 54 routed, 47 backed | 0 natively (8 via the Iceberg-REST dummy-table wrapper) | 15 | 10 | 0 (Parquet only) |
| `managed_versioning` | not yet advertised; backend `create_table_version` live | n/a | `false`, hard-coded | `false` | n/a |
| commit coordination for Lance | yes, on a non-spec `/commit` route, with conflict taxonomy and replay marker | no, by design | no | no | n/a |
| versions, tags, branches, indices, blobs, data plane | yes | no | no | no | n/a |
| authorization | OpenFGA, rask's own model with `validator`/`can_promote`, time-boxed grants | **OpenFGA**, near-isomorphic model, plus Cedar; custom `Authorizer` trait | jCasbin RBAC; pluggable engine, but grants stay in Gravitino's tables; zero OpenFGA mentions | jCasbin; authorizer hard-coded; fork to change | none (the database's) |
| lineage | OpenLineage graph in AGE, column lineage, storage reconcile | none; events for external tools | consumes and forwards OpenLineage; no store | none; roadmap ❓ | none |
| credential vending against RustFS | STS via web identity, per table and tier, with TTL | yes, storage profiles per warehouse | static keys on describe for the Lance path | no S3 endpoint override | none |
| hierarchy | project > warehouse > namespace > table | project > warehouse > namespace > table | metalake > catalog > schema > table, Lance REST limited to two levels | catalog > schema, fixed | schema > table in one database |
| background work | cron ticks, `asyncio.Lock`, `replicas: 1` | Postgres task queue with leases, heartbeats, retries | jobs subsystem (Shell, Spark) | none | none |
| soft-delete and undrop | trash records, purge off by default | yes, per-warehouse profiles, chained purge | tombstone GC only, no undrop | hard delete | snapshot time travel |
| relational catalog DB | none | Postgres 15+ | H2, MySQL or Postgres | H2, MySQL or Postgres | the catalog **is** the database |
| runtime added | none | Rust | JVM 17 | JVM 17 | C++ extension in-process |
| verdict under the locks | keep; relocate governance onto `CreateTableVersion`; adopt the backlog | design reference and FGA rung donor; not a substrate | conformance oracle for 15 ops; not a substrate | not a fit | disqualified |
