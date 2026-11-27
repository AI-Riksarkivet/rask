# Where a Lance catalog keeps state, and how commits are coordinated: what `lance_docs/` says and what pylance 14.0.3 does

Study for the governance-records move (ADR 0230). Findings only; no design recommendation.

**Sources.** `lance_docs/` at the pins in `lance_docs/PROVENANCE.md`: `lance/` = lance `main` at `f9fd19ed` (2026-11-22),
`ns_catalog/` = lance-namespace `main` at `a13c746b` (2026-10-23). Paths below are relative to `lance_docs/`. Every page
named in the brief was read in full; `spec.yaml` was read for every operation and schema the brief lists (lines
86-2300 paths, 2303-2392 parameters, 2393-5117 schemas).

**Measured on:** `lance.__version__ == "14.0.3"`, `lance-namespace 0.13.0`, Python 3.15, local filesystem, 2026-11-26.
`DirectoryNamespace` was built as `services/catalog/src/catalog/core/namespace.py:84` and `core/config.py:216-247` build it:
`DirectoryNamespace(session=lance.Session(), root=<dir>, inline_optimization_enabled="false", dir_listing_enabled="false")`
(a fresh `lance.Session()` stands in for `shared_lance_session()`; no `storage.*` keys, because the root is local).
Scripts and raw JSON: the measuring session's scratch directory, not kept.
(`measure_ops.py` → `ops.json`, `measure_commit.py` → `commit.json`; three further runs were inline and are quoted here in full).

Marker used: **DOCS ONLY (not on 14.0.3)** = documented, measured absent or different on the installed package.

---

## 1. Where may a catalog keep its own state?

### 1a. Sentences that leave the place to the implementation

| Where | Quote |
| --- | --- |
| `ns_catalog/namespace/supported-catalogs/lance-rest.md:25` | "A **child namespace** is managed by the REST server and accessed via namespace routes. The server is responsible for storing and organizing namespace metadata." |
| `lance-rest.md:29` | "**Namespace properties** are managed by the REST server and accessed via the DescribeNamespace operation." |
| `lance-rest.md:33` | "A **table** is managed by the REST server. The server handles table storage, versioning, and metadata management." |
| `lance-rest.md:37` | "The **table location** is managed by the REST server and returned in the DescribeTable response." |
| `lance-rest.md:39` | "**Table properties** are managed by the REST server and accessed via table operations." |
| `lance-rest.md:43-49` | "the server is responsible for managing Lance tables … The server implementation must ensure that: Tables are stored as valid Lance table directories on the underlying storage; The `location` field in DescribeTable response points to the Lance table root directory; Table properties include any Lance-specific metadata required by the Lance SDK" |
| `lance-rest.md:80`, `:157`, `:238`, `:320` | Each basic operation's steps say "the server's storage" ("Create the namespace in the server's storage", "Look up the namespace in the server's storage", "Reserve the table in the server's storage") without naming a store. |
| `ns_catalog/spec.yaml:3771-3773` (also `:3809-3810`, `:3845-3846`, `:1405-1406`, `lance-rest.md:661-663`) | CreateTable/DeclareTable `properties`: "Business logic properties stored and managed by the namespace implementation outside Lance context, if supported by the implementation." |
| `spec.yaml:2910-2912` | DescribeTable `properties`: "Properties stored on the table, if supported by the server. This records the information managed by the namespace." (contrast `:2900-2905`: `metadata` "records the information of the table and requires loading the table") |
| `spec.yaml:2516`, `:2591-2593`, `:2722` | Namespace and RegisterTable `properties`: "Properties stored on the namespace/table, if supported by the implementation." |
| `spec.yaml:102-104` | CreateNamespace: "the implementation may modify user-provided `properties`, such as adding additional properties like `created_at` … omitting any specific property, or performing actions based on any property value." |
| `spec.yaml:1979-1980` | DeclareTable: "Declare a table with the given name without touching storage. This is a metadata-only operation that records the table existence and sets up aspects like access control." |
| `spec.yaml:3747-3750` | CreateTableRequest: "The table location and any credential vending behavior are determined by the implementation and returned in the response, rather than specified in this request." |
| `spec.yaml:3833-3835` | DeclareTable `location`: "If not provided, the namespace implementation should determine the table location." |
| `spec.yaml:2473-2476` | `Context`: "Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation." (`catalog/rest/index.md:100` same) |
| `spec.yaml:2838-2843`, `:2886-2891` | `vend_credentials`: "When not set, the implementation can decide whether to return vended credentials." `storage_options` "may include vended credentials … the `expires_at_millis` key should be included". |
| `spec.yaml:4049-4051` | DropTable: "If the table and its data can be immediately deleted, return information of the deleted table. Otherwise, return a transaction ID that client can use to track deletion progress." (`:2639-2640` DropNamespace `transaction_id`: "operation is long running and should be tracked using DescribeTransaction") |
| `spec.yaml:3917-3952` | `TransactionStatus` (Queued/Running/Succeeded/Failed/Canceled) and `DescribeTransactionResponse.properties` (free map): the catalog keeps a transaction record whose store is unspecified. |
| `spec.yaml:684`, `:4721-4723` | ListTableVersions ordering "is implementation-defined" unless `descending=true`. |
| `ns_catalog/catalog/index.md:3` | "Catalog implementations vary widely across deployments, ranging from lightweight environments to enterprise platforms integrating with authorization systems or metadata services such as Apache Hive metastores." |
| `catalog/rest/index.md:3` | "connecting to metadata services or building a custom metadata server in a standardized way." |
| `catalog/rest/index.md:11` | "table commits are coordinated through the catalog before the resulting table metadata is written to storage. This enables organizations to enforce governance policies such as auditing, access control, and commit validation while still preserving the Lance table format as the authoritative source of table state." |
| `namespace/object-relationship.md:37-38` | "The root namespace is assumed to always exist … The lifecycle management (e.g. creation, deletion) of the root namespace is out of scope of this specification." |
| `namespace/supported-catalogs/template.md:28-38` | An implementation spec must state "Where namespace properties are stored", "Where table data is stored (location)", "Where table properties are stored" — i.e. the place is the implementation's to declare. |
| `namespace/supported-catalogs/index.md:25` | "Catalog implementations can be owned by external parties without needing to go through the Lance community voting process to be adopted." |
| `ns_catalog/catalog/dir/index.md:121` | "For example, implementations may store catalog-level properties in the `__manifest` table's metadata." |
| `catalog/dir/index.md:115` | "**Schema Extensibility**: The `__manifest` table schema may include additional columns beyond those listed above. Implementations should preserve unrecognized columns during updates, since extensions may add columns for filtering or other metadata-driven behaviors." |
| `lance/format/table/table_config.md:5-8` | Manifest `config`: "Keys prefixed with `lance.` are reserved for the Lance library itself. Other libraries building on Lance may define their own configuration keys, but should use their own prefix (e.g. `lancedb.`) to avoid collisions". `:12`: "Unrecognized keys … should be ignored by readers." |
| `table_config.md:16-18` | config "is distinct from schema metadata … and from table metadata (arbitrary metadata associated with the table)". |
| `lance/format/table/index.md:154-160` | `DataFragment.metadata`: "arbitrary metadata associated with the fragment … Use namespaced keys and small values … Changes to the same fragment preserve all entries, including unknown ones. New fragments do not automatically inherit metadata". Requires `FLAG_OBJECT_METADATA` (`:162-163`). |
| `lance/format/table/branch_tag.md:47`, `:127` | Branch and tag files each carry "`metadata` … String key/value metadata map". |
| `spec.yaml:4781-4785`, `:5098-5102`; `catalog/dir/index.md:177` | CreateTableVersion / TableVersion / dir version row: "Optional metadata for the version" / "Optional key-value pairs of version metadata". |

### 1b. Sentences that REQUIRE a place

| Where | Quote |
| --- | --- |
| `catalog/dir/index.md:73` | "V2 uses a special `__manifest` table (a Lance table) stored in the catalog directory to track all tables and namespaces." |
| `catalog/dir/index.md:105-111` | `__manifest` columns `object_id`, `object_type` ("Either `"namespace"` or `"table"`"), `location` ("only for tables"), `metadata` ("JSON-encoded metadata/properties (only for namespaces)"), `base_objects` ("Reserved for future use"). |
| `catalog/dir/index.md:113` | "`object_id` … is the unenforced primary key … Implementation of this spec must always enforce the primary key uniqueness using features like Lance merge insert with primary key deduplication." |
| `catalog/dir/index.md:119` | "root namespace properties are stored in the `__manifest` Lance table's metadata map." |
| `catalog/dir/index.md:133-134` | "When adding a new entry in the manifest table, it must atomically check if the table already exists such entry, as well as if any concurrent operation writes the same entry, and fail the operation accordingly". |
| `catalog/dir/index.md:138` | V2 table directories are "`<hash>_<object_id>`". |
| `catalog/dir/index.md:153-166` | Managed versioning: "store `table_version_management=true` in the `__manifest` Lance table's metadata map … Table versions are stored in the `__manifest` table with `object_id` in the format `<table_id>$<version>` … The `object_type` for table version entries is `"table_version"`." |
| `catalog/dir/index.md:52-69` | V1 markers: `.lance-reserved` ("reserved but not yet created"), `.lance-deregistered` ("deregistered but data is preserved"). |
| `namespace/supported-catalogs/lance-dir.md:33` | "**Namespace properties** are stored as JSON in the `metadata` column of the manifest table." |
| `lance-dir.md:47` | "**Table properties** are stored in Lance table metadata and can be accessed via the Lance SDK." |
| `spec.yaml:462-464`, `:1982-1983` | DirectoryNamespace: a table exists if it has Lance versions or "A `.lance-reserved` file exists in the table directory"; DeclareTable "creates a `.lance-reserved` file". |
| `ns_catalog/partitioning-spec.md:51-54`, `:164` | A partitioned namespace is identified by, and stores its specs in, "the `__manifest` table's metadata map". |
| `lance/format/table/branch_tag.md:26`, `:103` | Branches at `_refs/branches/{name}.json`, tags at `_refs/tags/*.json`; "Tags are always stored at the root dataset level" (`:114`). |
| `lance/format/table/layout.md:218` (Transaction Files) | `_transactions/{read_version}-{uuid}.txn`. |

### 1c. Measured: where `DirectoryNamespace` 14.0.3 actually puts things

| Call | Observed |
| --- | --- |
| `create_namespace(["n"], properties={"nk":"nv"})`; `create_table(["n","t"], properties={"owner":"x"})` | `__manifest` rows: `{'object_id':'n','object_type':'namespace','location':None,'metadata':'{"nk":"nv"}'}`, `{'object_id':'n$t','object_type':'table','location':'049d69b8_n$t','metadata':'{"owner":"x"}'}`. The table's Lance `table_metadata` is `None`, `config()` is `{}`, schema metadata `None`. |
| `describe_table(["n","t"], load_detailed_metadata=True)` | `properties={'owner':'x'}`, `metadata=None`. |
| `declare_table(["n","d"], properties={"p":"1"})` | Table dir holds only `.lance-reserved`; `describe_table(check_declared=True)` → `properties={'p':'1'}, is_only_declared=True`. |
| `__manifest` schema | `object_id: string not null` (field metadata `lance-schema:unenforced-primary-key:position: '0'`), `object_type: string not null`, `location`, `metadata`, `base_objects: list<string>`. No `created_at` column; schema metadata `None`. |

So on 14.0.3 **table properties live in the `__manifest` `metadata` column**, contradicting `lance-dir.md:47` ("stored in Lance table metadata") and `dir/index.md:110` ("only for namespaces"). `lance-dir.md:72` ("`created_at` set to the current timestamp") — **DOCS ONLY (not on 14.0.3)**: no such column, and the namespace row's `metadata` holds only the caller's properties.

---

## 2. The two catalog shapes

### 2a. Directory Catalog: atomicity per operation (`lance-dir.md`, V2 unless noted)

General rule: `catalog/dir/index.md:81` "Because the catalog metadata is itself stored as a Lance table, the catalog inherits the transactional semantics, snapshot isolation, and schema evolution guarantees of the table format"; `:133-134` (quoted in 1b).

| Operation | How it is made atomic (quote) | What is said NOT to be atomic / multi-step |
| --- | --- | --- |
| CreateNamespace (`:61-82`) | "Merge-insert a new row … Primary-key deduplication on `object_id` ensures no duplicate rows are inserted." (`:68-74`) | Parent check (`:67`) is a separate prior step; nothing said about a concurrent drop of the parent. |
| ListNamespaces / DescribeNamespace / ListTables / DescribeTable (`:84-207`) | Reads of the `__manifest`. | — |
| DropNamespace (`:115-130`) | "Delete the namespace row … using the `object_id` primary key" (`:124`) | Child check (`:122-123`) and delete are separate steps; no atomicity statement. |
| DeclareTable (`:132-157`) | "Create a `.lance-reserved` file … In V2, merge-insert a row … Primary-key deduplication" (`:143-149`); concurrent creation → error 14 (`:157`). | Two writes (marker file, then row); no statement covers a failure between them. |
| DeregisterTable (`:209-242`) | V2: "Remove the table row … Keep the table files" (`:229-232`). | — |
| DropTable (`:246-275`) | V2: "Remove the table row … Delete the table directory … (failure here does not affect the success of the drop since the table is no longer reachable)" (`:258-261`). | V1: "If deletion fails midway … the drop has failed and should be retried" (`:254`). |
| CreateTableVersion, management off (`:281-288`) | "Copy the staging manifest to the final path in the `_versions/` directory using `put_if_not_exists` semantics" (`:286`), then "Delete the staging manifest file" (`:287`). | Copy and delete are two steps. |
| CreateTableVersion, management on (`:290-300`) | "Merge-insert a new row … `object_id` set to `<table_id>$<version>` … The commit is effectively complete after this step." (`:293-298`) | Steps 3-4 (copy to `_versions/{version}.manifest`, update pointer) follow the commit. |
| BatchCreateTableVersions (`:310-334`) | On: "Merge-insert all version rows into the `__manifest` table in a single atomic commit … If any version already exists, the entire batch fails." (`:319-324`) | Off: "iterates through each entry and calls `CreateTableVersion` for each one. Atomicity is not guaranteed." (`:314`) |
| DescribeTableVersion, on (`:374-379`) | Repairs a staging path: "copy the staged manifest to its final location … This is an idempotent operation." (`:377`) | — |
| BatchDeleteTableVersions (`:387-407`) | On: delete manifest files, then rows (`:399-401`). | Two steps; no atomicity statement. |
| RenameTable | **Not described in `lance-dir.md`** (no section). `dir/index.md:78` lists "tables can be renamed" as a V2 capability; `:201` "If a table in the root namespace is renamed, it transitions to the V2 hash-based path naming." | — |
| Tags, branches, RestoreTable, transactions | Not described in `lance-dir.md`. | — |

### 2b. REST Catalog: required vs left open

Required of a server: route grammar and the reverse-proxy principle (`catalog/rest/index.md:35-42`); "When the information in the route and request body both present but do not match, the server must throw a 400 Bad Request error. When the information in the request body is missing, the server must use the information in the route instead." (`:49-50`); identity header mapping, "header values take precedence" (`:52-64`); context `header.` mapping (`:66-98`); the non-standard bodies (`:107-346`); the ErrorResponse and error-code-to-HTTP mapping "REST Catalog implementations must map Lance error codes to HTTP status codes as follows" (`lance-rest.md:1000-1010`); `lance-rest.md:45-49` (table directory valid, location is the root). `CreateTableVersion` "supports `put_if_not_exists` semantics. The operation will fail with 409 Conflict if the version already exists." (`spec.yaml:736-737`). `BatchCreateTableVersions` / `BatchCommitTables` "atomic: either all … or none" (`spec.yaml:850-852`, `:901`). AlterTransaction "should either succeed and apply all actions, or fail and apply no action" (`spec.yaml:2277`).

Left open: where namespaces, tables, properties, locations and versions are stored (section 1a), the "Recommended Basic Operations" are a recommendation (`namespace/operations/index.md:97-118`), DropNamespace Cascade and detailed DescribeTable are not required (`:120-131`), version ordering, credential vending, context use.

### 2c. A REST server that uses a Directory Catalog internally

- Permitted and described as a pattern, not for `dir` by name: `catalog/rest/index.md:350` "Any REST HTTP server that implements this OpenAPI protocol is called a **Lance REST Catalog server**." `:356-358` a server that proxies an existing metadata service "is called a **Lance Catalog adapter**." `:362-365` "Any adapter can always be directly a Lance catalog implementation bypassing the REST server, and vise versa. In fact, an implementation is basically the backend of an adapter. For example, we natively support a Lance HMS Catalog implementation, as well as a Lance catalog adapter for HMS by using the HMS Catalog implementation to fulfill requests in the Lance REST server."
- `namespace/operations/index.md:12-16`: execution may be "Client-side … (e.g., directory namespace)", "Server-side", or "Hybrid".
- `spec.yaml:28-30`: the paths section is "for the Lance REST Namespace implementation, which defines a complete REST server that can work with Lance datasets."
- No page names the Directory Catalog as a REST server's backend. grep `director|\bdir\b|backend|adapter` over `catalog/rest/index.md`, `lance-rest.md`, `namespace/index.md`, `namespace/operations/index.md` finds only the HMS example. **Verdict: a permitted configuration (implementation-as-backend-of-adapter), described generically with HMS as the example; the dir-backed case is not mentioned.**

---

## 3. Rename

**Spec.** `spec.yaml:597-599` RenameTable: "Rename table `id` to a new name." Request (`:4632-4653`): required `new_table_name`; optional `new_namespace_id` "New namespace identifier to move the table to (optional, if not specified the table stays in the same namespace)". Response: `transaction_id` only (`:4655-4662`). Errors 1, 4, 5, 14 (`namespace/operations/errors.md:75`). `lance-rest.md:462-485` shows a body `{"new_id": [...]}`, which the spec's schema does not have (the prose is not the route authority, PROVENANCE). **No namespace rename operation exists** in `spec.yaml` (54 paths, listed in the operation table `namespace/operations/index.md:42-95`; none renames a namespace).

**Directory docs.** Only `dir/index.md:78` and `:201` (quoted in 2a). No procedure, no atomicity statement.

**Measured** (`ops.json`, `commit.json`): every method `dir(DirectoryNamespace)` lists, called once. "inherited" = not defined on `lance.namespace.DirectoryNamespace`, so `lance_namespace.LanceNamespace`'s default runs (4 of 58: `batch_commit_tables`, `batch_create_table_versions`, `create_empty_table`, `update_field_metadata`).

| Call | Outcome | Answer (abridged) |
| --- | --- | --- |
| `namespace_id()` | OK | `DirectoryNamespace { root: "…" }` |
| `create_namespace(id=["ns1"], properties={"k":"v"})` | OK | `{"properties":{"k":"v"}}` |
| `list_namespaces(id=[])` | OK | `{"namespaces":["ns1","ns2"]}` |
| `describe_namespace(["ns1"])` / `namespace_exists(["ns1"])` | OK / OK | `{"properties":{"k":"v"}}` / `{}` |
| `create_table(["ns1","t1"], properties={"owner":"x"}, ipc)` | OK | `location=…/82df7981_ns1$t1, version=1, properties={"owner":"x"}` |
| `declare_table(["ns1","t2"])` | OK | `location=…/58bfac77_ns1$t2` |
| `create_empty_table` (inherited) | UnsupportedOperationError | `Not supported: create_empty_table` |
| `list_tables(["ns1"])` | OK | `{"tables":["t1","t2"]}` |
| `list_all_tables(ListTablesRequest())` and `(id=[])` | OK | `{"tables": []}` **with one or two tables present** (both runs) |
| `describe_table(load_detailed_metadata=True)` | OK | `table, namespace, version=1, location, table_uri, schema…`; no `managed_versioning` |
| `table_exists` | OK | `{}` |
| `insert_into_table(mode=append)` | OK | `{"num_inserted_rows":3,"version":2}` |
| `merge_insert_into_table(on=["id"], update_all, insert_all)` | OK | `{"num_updated_rows":6,…,"version":3}` |
| `update_table(predicate="id = 1", updates=[["v","'z'"]])` | OK | `{"updated_rows":2,"version":4}` |
| `delete_from_table("id = 2")` | OK | `{"version":5}` |
| `query_table(k=2, vector)`, `count_table_rows`, `explain_…`, `analyze_…`, `get_table_stats` | OK ×5 | Arrow IPC file; `{"count":4}`; plan strings; stats |
| `alter_table_add_columns` / `alter_columns` (rename) / `drop_columns` | OK ×3 | versions 6, 7, 8 |
| `alter_table_backfill_columns` | NotImplementedError | `Not supported: alter_table_backfill_columns not implemented, …rust/lance-namespace/src/namespace.rs:471` |
| `update_table_schema_metadata({"a":"b"})` | OK | `{"metadata":{"a":"b"},"transaction_id":"b28fc896-…"}` |
| `update_field_metadata` (inherited) | UnsupportedOperationError | `Not supported: update_field_metadata` |
| `create_table_scalar_index(BTREE)`, `create_table_index(IVF_FLAT)`, `list_table_indices`, `describe_table_index_stats`, `drop_table_index` | OK ×5 | each mutation answers `transaction_id` |
| `create/list/get/update/delete_table_tag` | OK ×5 | `list` → `{"t":{"version":1,"manifest_size":537}}` |
| `create/list/delete_table_branch` | OK ×3 | `{"b1":{"parent_version":12,"create_at":…,"manifest_size":948}}` |
| `list_table_versions(descending=True)` | OK | entries carry `version, manifest_path, manifest_size, e_tag, timestamp_millis` |
| `describe_table_version(DescribeTableVersionRequest(...))` | TypeError | `'DescribeTableVersionRequest' object is not an instance of 'Mapping'` |
| `describe_table_version({"id":T,"version":1})` | OK | `{"version":{"version":1,"manifest_path":…}}` |
| `create_table_version(CreateTableVersionRequest(...))` | TypeError | not a `Mapping` (14.0.3 takes a dict) |
| `create_table_version({dict})` | OK / ConcurrentModificationError | see section 4c |
| `batch_create_table_versions` (inherited) | UnsupportedOperationError | `Not supported: batch_create_table_versions` |
| `batch_commit_tables` (inherited) | UnsupportedOperationError | `Not supported: batch_commit_tables` |
| `restore_table(version=1)` (clean table) | OK | `{"transaction_id":"2ae5fbdb-…"}` |
| `batch_delete_table_versions({dict} ranges=[{1,2}])` | OK | `{"deleted_count":1}`; version 1 gone from `list_table_versions` (model request → TypeError, as above) |
| `describe_transaction(id=["x"])` | InvalidInputError | `Transaction request id must include table id and transaction identifier` |
| `describe_transaction(id=[*T, <uuid>])` and `(id=[*T,"3"])` | OK | `{"status":"SUCCEEDED","properties":{"version":"3","read_version":"2","operation":"UpdateConfig","uuid":"fd69e109-…"}}` |
| `alter_transaction(id=[*T,<uuid>], setStatus Canceled)` | OK | `status: SUCCEEDED` (unchanged); wrote `_alter_transactions/<uuid>.json` in the table directory |
| `create_materialized_view` / `refresh_materialized_view` | NotImplementedError ×2 | `not implemented, …namespace.rs:492` / `:481` |
| **`rename_table(["ns1","t1"], new_table_name="t1r")`** | **NotImplementedError** | **`Not supported: rename_table not implemented, /home/runner/work/lance/lance/rust/lance-namespace/src/namespace.rs:292:13`**; root listing identical before and after |
| **`rename_table(["ns1","t1r"], new_table_name="t1m", new_namespace_id=["ns2"])`** | **NotImplementedError** | same message |
| `deregister_table(["ns1","t2"])` | OK | `{"id":["ns1","t2"],"location":…}`; directory kept |
| `register_table(location=<absolute file:// URI>)` | InvalidInputError | `Absolute URIs are not allowed for register_table. Location must be a relative path within the root directory` |
| `register_table(["n","t_reg"], location="d34313cd_n$t")` (relative, an existing table's dir) | OK | then `list_tables(["n"])` → `["t","t_reg"]`: two ids, one location |
| `drop_namespace(["ns1"])` (Restrict, non-empty) | NamespaceNotEmptyError | `contains 1 child objects` |
| `drop_namespace(["n"], behavior="Cascade")` (one table inside) | NamespaceNotEmptyError | `contains 1 child objects`; table still describable |
| `drop_namespace(["ns2"], behavior="Cascade")` (empty) | OK | `{}` |
| `retrieve_ops_metrics()` / `reset_ops_metrics()` | OK | `{}` / `null` |

`rename_table` is **DOCS ONLY (not on 14.0.3)** for the directory backend (`dir/index.md:78`, `:201`). DropNamespace `Cascade` (`spec.yaml:2625`) is likewise not honoured by the 14.0.3 dir backend (refused as non-empty). The 15.0.0 change "`create_table_version`, `describe_table_version`, and `batch_delete_table_versions` now always return the corresponding `lance_namespace` response model" (`lance/guide/migration.md:13-21`) is **DOCS ONLY (not on 14.0.3)**: these three take a dict, and a model request raises `TypeError`.

---

## 4. Commit coordination

### 4a. The External Manifest Store, as documented (`lance/format/table/transaction.md:960-1042`)

- Purpose and authority: "If the backing object store does not support atomic operations … an external manifest store can be used to enable concurrent writers." (`:962`) "An external manifest store is a key-value store that supports put-if-not-exists operations. It is the concurrency coordinator and fast version index: its conditional write selects one immutable staging manifest for each version. The canonical manifest bytes in object storage remain authoritative, so the external store supplements but does not replace them." (`:964-967`)
- Unaware reader: "A reader unaware of the external manifest store can still read the table, but may observe a version up to one commit behind the true latest version." (`:968`)
- What it holds per version: a key `base_uri, version` → a path, first the staging path `{dataset}/_versions/{version}.manifest-{uuid}` (`:980`), then the final path (`:990`); after the change, "the same stable path-and-size tuple" (`:997`).
- Steps (`:976-998`):
  1. "Stage manifest: `PUT_OBJECT_STORE {dataset}/_versions/{version}.manifest-{uuid}` … not yet visible to readers".
  2. "Reserve version in external store: `PUT_EXTERNAL_STORE base_uri, version, …manifest-{uuid}` … put-if-not-exists … The reservation selects one immutable staging object; it is not yet the canonical commit … If this operation fails due to conflict, another writer reserved this version".
  3. "Finalize in object store: `COPY_OBJECT_STORE … → {dataset}/_versions/{version}.manifest` … **Successful materialization at this deterministic path is the commit point** … makes the manifest discoverable by readers unaware of the external store".
  4. "Update external store pointer … After copying, read the canonical object's current metadata. Return its ETag to the caller as an opaque physical-generation observation … **Do not persist that ETag in the external store.** Concurrent finalizers can copy the same selected immutable bytes into different physical generations, and COPY plus external-store publication is not atomic."
- Fault tolerance (`:1002-1006`): after 2 before 3 → "pending reservation. Readers that use the external store detect this state and retry materialization." After 3 before 4 → "the canonical object remains committed; readers use it and may repair the external index. Staging deletion is garbage collection and does not affect the commit outcome."
- ETag rules for readers (`:1022-1027`): "If the path does not end with a UUID, validate the canonical object's size. Ignore any legacy stored ETag because it is neither content identity nor dataset-incarnation identity". Repair "Best-effort record the finalized path and size without an ETag" (`:1034`); "If canonical materialization cannot be established, or an observed size differs, return an error" (`:1040`).
- Portability: "copying the dataset directory preserves all data without requiring the external store." (`:1042`). Overview: "the external system helps serialize commits and apply governance checks, while the canonical table state is still persisted in the Lance table format." (`lance/format/table/index.md:21`)

### 4b. How the REST table-version operations map onto it

- `catalog/rest/index.md:11` and `catalog/index.md:25`: the version APIs "can act as an external manifest store".
- `managed_versioning` (`spec.yaml:2920-2925` DescribeTable, `:3879-3884` DeclareTable): "When true, the caller should use namespace table version operations (CreateTableVersion, BatchCreateTableVersions, DescribeTableVersion, ListTableVersions, BatchDeleteTableVersions) to manage table versions instead of relying on Lance's native version management."
- `CreateTableVersionRequest` (`:4742-4787`): required `version`, `manifest_path` ("Path to the manifest file for this version"); optional `branch`, `manifest_size`, `e_tag` ("Optional ETag for the manifest file"), `metadata` ("Optional metadata for the version"), `naming_scheme` (V1 `_versions/{version}.manifest`, V2 `{u64::MAX - version}` zero-padded, `:5104-5116`). Response (`:4789-4799`): `transaction_id`, `version: TableVersion` (`version`, `manifest_path`, `manifest_size`, `e_tag` "Optional ETag for optimistic concurrency control", `timestamp_millis`, `metadata`; `:5070-5102`).
- Server duty: "supports `put_if_not_exists` semantics … will fail with 409 Conflict if the version already exists" (`:736-737`).
- "Staging manifest": the spec never uses the word; `lance-dir.md:284-299` and `lance-rest.md:501` do: a manifest the caller writes to `{table_location}/_versions/{version}.manifest-{uuid}` that "is not yet visible to readers" (`lance-dir.md:292`).
- **The two trees disagree on the commit point.** `lance-dir.md:298` (ns_catalog, 2026-10-23): the `__manifest` merge-insert is the commit — "The commit is effectively complete after this step." `transaction.md:982-987` (lance, 2026-11-22): the reservation "is not yet the canonical commit"; materialization at the deterministic path "is the commit point". Likewise on ETags: `dir/index.md:176` stores `e_tag` per version and `spec.yaml:5089-5093` calls it "for optimistic concurrency control", while `transaction.md:995` says "Do not persist that ETag" and `:1010-1014` (Rolling Upgrade) "New readers ignore legacy stored ETags".

### 4c. What exists on pylance 14.0.3 (measured)

| Probe | Result |
| --- | --- |
| `inspect.signature(lance.write_dataset)` / `lance.dataset` / `LanceDataset.commit` | All take `commit_lock: Optional[CommitLock]`; `commit` also `max_retries: int = 20`, `commit_timeout = timedelta(seconds=1800)`, `namespace_client`, `table_id`, `namespace_client_managed_versioning: bool = False`, `detached`. `commit_batch(dest, transactions, commit_lock, …, max_retries=20)` takes one `dest`. |
| `commit_lock` callback | `write_dataset(..., commit_lock=lambda v: Lock(v))` create then append: called with `[1, 2]` (the version being committed). Docs: `lance/guide/object_store.md:431-446`. |
| Python hook for an external manifest store | None found in the Python API. The binary `lance.abi3.so` contains the symbols `ExternalManifestCommitHandler`, `DynamoDBExternalManifestStore`, `LanceNamespaceExternalManifestStore`, `PyCommitLock` (strings); that the namespace-managed path is implemented as a `LanceNamespaceExternalManifestStore` is an inference from the name, not confirmed. |
| Dir property for managed versioning | The binary's dir property list is `dir_listing_enabled, inline_optimization_enabled, table_version_tracking_enabled, dir_listing_to_manifest_migration_enabled, commit_retries, vend_input_storage_options…`; the string `table_version_management` occurs nowhere in it (0 hits). |
| `DirectoryNamespace(..., table_version_tracking_enabled="true")` | `describe_table` and `declare_table` answer `managed_versioning: true`. `write_dataset(namespace_client=nsB, table_id=T, mode="append")` → version 2. `__manifest` versions 1-4 hold only `n:namespace`, `n$t:table`, `n$t2:table`; **no `table_version` row at any version**; `__manifest` schema metadata `{}`; `_versions/` holds final manifests only. |
| `table_version_management=true` in `__manifest` metadata + `<table_id>$<version>` rows (`dir/index.md:153-178`, `lance-dir.md:290-300`) | **DOCS ONLY (not on 14.0.3)**: key absent from the binary, and the installed flag writes no version rows. |

**A pylance client facing `managed_versioning=true`** (a recording `LanceNamespace` that delegates to a plain dir backend and sets `managed_versioning=True` on describe/declare, which is rask's shape):

- Create (`write_dataset(mode="create", namespace_client=…)`): `declare_table({'id':['n','t']})` → `list_table_versions({'id':…,'limit':1,'descending':True})` twice → `create_table_version({'id':['n','t'],'version':1,'manifest_path':'<root without scheme>/b0f4590f_n$t/_versions/18446744073709551614.manifest-72aa9054-…','manifest_size':387,'e_tag':'"b9c60c6-…"','naming_scheme':'V2'})`. At that call `_versions/` holds only the staged file; after the backend: only `18446744073709551614.manifest` (staged moved, not copied).
- Append: `describe_table` → `list_table_versions(limit 1, descending)` → `create_table_version(version=2, manifest_path=…/_versions/18446744073709551613.manifest-c12ac33d-…, manifest_size=473, e_tag=…, naming_scheme='V2')` → answer `{'version':{'version':2,'manifest_path':…/18446744073709551613.manifest,'manifest_size':473,'e_tag':…}}`.
- The staged name is the **V2 inverted name plus `-<uuid>`**, not the `{version}.manifest-{uuid}` of the docs (`transaction.md:976`, `lance-dir.md:292`); `manifest_path` is an object-store path without scheme; the request is a `DictWithModelDump`, not a model.
- A namespace that answers `create_table_version` with `ConcurrentModificationError`: pylance calls `describe_table_version({'id':…,'version':3})` four times, then raises `OSError: Commit result for version 3 is unknown: the commit may or may not have been applied; check the table state before retrying: commit reported a conflict but the manifest at the target version could not be read back for verification` (`rust/lance/src/io/commit.rs:1990`). The staged `_versions/18446744073709551612.manifest-c33c009b-…` stays behind; the `.lance-reserved` the create wrote also stays after version 1 exists.
- The dir backend's `create_table_version` (management off) **does not validate the staged manifest's own `version` field**: a detached manifest (`version` = `14757301989231210956`) staged as `13.manifest-…` and submitted as version 13 was accepted and landed as version 13; the same request again → `ConcurrentModificationError: Version 13 already exists … with different content`; a later `restore_table(version=1)` on that table failed `more than 2^65 versions have been created and so regular version numbers are appearing as 'detached' versions` (`rust/lance/src/io/commit.rs:1848`). An existing version is answered with code 14, not 12.

### 4d. rask today vs the documented protocol (`services/catalog/src/catalog/services/managed_versions.py`)

rask's REST server answers `managed_versioning = True` (`api/v1/endpoints/tables.py:465`, `:647`; `services/tables/commits.py:608`); its own `DirectoryNamespace` runs without `table_version_tracking_enabled` (no occurrence in `services/` or `packages/` outside tests), so its finalize is the management-off path of `lance-dir.md:281-288`.

| Documented step (`transaction.md:976-998`; REST `spec.yaml:736-737`) | rask (`managed_versions.py`) | Match / differ |
| --- | --- | --- |
| 1. Client stages `…manifest-{uuid}`, invisible to readers. | pylance stages under the ref's `_versions/` (or `tree/<branch>/`); `_staged_uri` (`:161-172`) accepts only a staged name directly under the ref's `_versions/`. | Match (name format is pylance's V2 form, section 4c). |
| 2. Reserve `version → staging path` in an external KV store with put-if-not-exists. | No external store and no reservation. `stage` (`:225-283`) reads the ref's latest version by opening it (`_latest_version`, `:175-194`), answers `VersionTakenError` (409) for `version <= latest` and 400 for `version > latest + 1`, refuses a naming scheme other than V2, and refuses a manifest whose stated version differs from the request. | Differs: a read-then-check, not an atomic reservation. |
| (between 2 and 3) "commit validation" (`catalog/rest/index.md:11`). | Plants two catalog-written copies as detached manifests `_versions/d<id>.manifest`: one with its `version` field rewritten to the detached id, opened through pylance and judged (`judge`, `:906`), one byte-identical for the finalize (`:248-249`, docstring `:8-14`). Reads the staged transaction's uuid, properties, read version (`:290-316`). | Validation is a documented purpose of the external store; the detached-copy mechanism is rask's own. |
| 3. Copy staged → `_versions/{version}.manifest`; materialization is the commit point. | `finalize` (`:1014-1046`) calls the dir backend's `create_table_version` with `manifest_path` = the catalog's finalize copy; the backend moves it into the V2 slot with put-if-not-exists (`lance-dir.md:286`; measured: staged file gone, final present). The object store's conditional put arbitrates two writers ("two writers racing for one version number meet the store's arbitration", docstring `:12-14`). | Match on the commit point (canonical path, CAS by the object store); differs in that the bytes moved are a catalog copy, not the client's staged object (byte-identical by construction). |
| 4. Update external pointer; return ETag; do not persist it. | No pointer to update. Deletes the client's staged manifest after landing (`_delete_staged`, `:1062-1064`); on failure deletes its finalize copy (`refuse`). The response is the backend's `CreateTableVersionResponse`. Records an author keyed by the transaction uuid outside the protocol (`StagedVersion.transaction_uuid`, `forget_unlanded_author`, `:1049-1059`, ADR 0187). | No external index exists to repair; staged deletion is GC as `transaction.md:1005-1006` allows. Author recording has no counterpart in the docs. |
| Lost race → 409. | The backend's 409 (`ConcurrentModificationError`); rask hands the writer the next "commit turn" (`writer_turn`, `:214-222`) and waits rather than refusing, because "pylance fails a commit whose finalize answered 409 with no version at its target as of unknown outcome" (docstring `:1036-1038`; measured again in 4c on 14.0.3). | Commit turns are rask's own; nothing in the docs orders writers this way. |
| Reader unaware of the store "may observe a version up to one commit behind" (`:968`). | The finalized manifest is at the canonical path before the answer, so a plain reader sees it. | The lag does not arise (no external index). |

---

## 5. Per-commit bookkeeping

- **Transaction fields.** "Each transaction contains a `read_version` field …, a `uuid` field uniquely identifying the transaction, and an `operation` field" (`transaction.md:80-81`). The protobuf bodies are unrendered placeholders (`%%% proto.message.* %%%`, PROVENANCE: 71 of them), so `transaction_properties` is **not documented in the format pages**; grep `transaction_properties` over `lance/format` and `lance/guide` → 0 hits. pylance exposes it (`write_dataset(..., transaction_properties=…, commit_message=…)`).
- **Measured:** `write_dataset(…, transaction_properties={"who":"me"}, commit_message="msg")` → `read_transaction(v)` = `{"uuid":"85947ca8-…","read_version":2,"properties":{"who":"me","__lance_commit_message":"msg"}}`: the commit message is stored as the property `__lance_commit_message`. The same through the managed-versioning namespace path: `{"rask.run":"r1","__lance_commit_message":"hello"}`, operation `Append`. `versions()[-1]` = `{"version", "timestamp", "metadata": {total_rows, total_fragments, …}}` — no author field.
- **Transaction files.** "store the serialized transaction protobuf message for each commit attempt … 1. Enable manifest reconstruction during commit retries … 2. Support conflict detection" (`transaction.md:35-39`); path `_transactions/{read_version}-{uuid}.txn` (`layout.md:218-222`); inline copy at manifest field 23, "Decoding an inline transaction while opening a manifest is an optional cache optimization" (`transaction.md:43-47`); `FLAG_DISABLE_TRANSACTION_FILE` (`2^5`) "Transactions are recorded in the manifest rather than in a separate transaction file." (`versioning.md`, flag table). Measured on 14.0.3: every commit wrote a `_transactions/*.txn` (e.g. `0-1e713085-….txn`, `1-ea55d228-….txn`).
- **Who may write a transaction file:** nothing found (grep `transaction file|_transactions` over both trees: only the purpose, path and flag above).
- **Lifetime:** the docs say nothing explicit; `cleanup_old_versions` "deletes old version metadata and any data files that are no longer referenced" (`lance/guide/read_and_write.md:872-873`). PROVENANCE (measured on 13.0.0, held on 14.0.3): "`cleanup_old_versions` deletes `_transactions/`", and the 14.0.3 `CleanupStats` carries `transaction_files_removed` (measured).
- **Recording who committed:** no field for an author in the format. The only author-shaped text is an example: `dir/index.md:182-189` version metadata `"metadata": {"author": "user1", "description": "Initial schema"}`; the REST version `metadata` map (`spec.yaml:4781-4785`). Identity is a request field mapped to headers (`catalog/rest/index.md:52-64`, `spec.yaml:2451-2470`), not persisted by the spec. The namespace `transaction_id` on the dir backend is the Lance transaction uuid (measured: `update_table_schema_metadata` → `transaction_id: fd69e109-…`, and `describe_transaction([*T, that uuid])` → `properties {uuid, read_version, version, operation}`).
- **Ordering of concurrent writers.** Five-line summary of the conflict matrix (`transaction.md:83-626`, `:836-958`):
  1. Append conflicts only with Overwrite, Restore, UpdateMemWalState (`:105-111`); appends never conflict with each other.
  2. Delete/Update rebase against each other unless they touch the same rows (`:145-150`, `:438-442`); against Rewrite/DataReplacement on overlapping fragments they are retryable (`:139-143`).
  3. Overwrite and Restore trump almost everything; Restore conflicts only with UpdateMemWalState and schema-touching UpdateConfig (`:361-365`).
  4. Merge (add columns) is "overly general": retryable against every data write, conflicts with Project (`:290-313`); schema changes "will conflict with most other concurrent write operations" (`lance/guide/data_evolution.md:10-14`).
  5. CompositeOperation vs any named operation "fails closed" (`transaction.md:832-834`); without `AssertCellsUnchanged` this is snapshot isolation (`:807-814`).
  Outcomes: rebasable (retried "automatically within the commit layer"), retryable ("the application should re-read the data and retry"), incompatible (`:843-852`). "Retries are handled automatically but they repeat work" (`lance/guide/performance.md:443-445`). **Backoff:** nothing found (grep `backoff` over `lance/format`, `lance/guide`, `ns_catalog`: 0 hits).
  **Anything beyond optimistic retry:** only external coordination — a shared distributed `commit_lock` / Rust `CommitLock` or `CommitHandler` (`object_store.md:418-446`, `:646-653`), the external manifest store (`transaction.md:962`), and the GooseFS note's "single-writer queue, table-level lock, or a gateway that serializes commits" (`object_store.md:724-729`). Nothing in either tree orders two writers fairly or by arrival.

---

## 6. Tags, branches and clean-up

- **What protects a version.** "Tagged versions are exempted from the `LanceDataset.cleanup_old_versions()` process. To remove a version that has been tagged, you must first `LanceDataset.tags.delete()` the associated tag." (`lance/guide/tags_and_branches.md:79-83`). "Tags, current versions and branch dependencies remain protected." (`read_and_write.md:938`). "Cleanup will never delete the current (active) version." (`:1006`). "Branches hold references to data files. Lance ensures that cleanup does not delete files still referenced by any branch." (`tags_and_branches.md:233`). "Protected snapshots include the latest version, retained history, tags, and branch references under the table's cleanup policy." (`lance/format/table/blob.md:290-291`). Config `lance.auto_cleanup.referenced_branch` "Whether automatic cleanup also removes eligible old versions that are only reachable through a branch or tag reference. Defaults to `false`." (`table_config.md:35`). Unverified files: removed only if "at least **7 days** old" unless `delete_unverified=True` (`read_and_write.md:997-1004`, `:1020-1029`).
- **Measured:** tag `keep` on v1, `cleanup_old_versions(older_than=0)` → `OSError: Cleanup error: 1 tagged version(s) have been marked for cleanup. Either set error_if_tagged_old_versions=false or delete the following tag(s)`; with `error_if_tagged_old_versions=False` → removes nothing, versions `[1, 2]` kept.
- `CleanupPolicy`, `KeepOne`, `RemoveAll` and `lance.auto_cleanup.policy*` (`read_and_write.md:905-982`): **DOCS ONLY (not on 14.0.3)** — `lance` exports none of the three; `cleanup_old_versions(older_than, retain_versions, *, delete_unverified, error_if_tagged_old_versions, delete_rate_limit, versions, delete_concurrency)`.
- **Guard between a running clean-up and a concurrent tag/branch write:** nothing found. Tags and branches are outside the commit protocol: "Creating or deleting tags does not generate new dataset versions. Tags exist as auxiliary metadata stored in a separate directory." (`tags_and_branches.md:27-28`; branches `:93-94`). The only retention-race text is for reused objects: "When reusing an existing object, the source snapshot must remain protected from cleanup until the destination commit completes. Establish retention, for example through a source tag, before cleanup can retire it. An open handle or an existence check does not pin the snapshot … manifest publication alone provides no additional lease for the preceding work." (`blob.md:259-264`). PROVENANCE lists "a tag created during a cleanup" as measured on 12/13 and not re-measured on 14.0.3; not measured here either.
- **Branch deletion.** Docs: "Delete unused branches to allow their referenced files to be cleaned up by `cleanup_old_versions()`." (`tags_and_branches.md:235`); `branch_tag.md` gives no delete semantics. Measured: after `create_branch("b1")` + one append on it, `branches.delete("b1")` removed `_refs/branches/b1.json` and every `tree/b1/**` file (manifests, transactions, the branch's own data file) at once; `_refs/tags/keep.json` stayed.
- **Shallow clone.** "Each branch dataset is technically a shallow clone of the source dataset." (`branch_tag.md:51`). `layout.md:170-176`: "Original fragments reference source via `base_id: 1` … Source dataset remains immutable and can be garbage collected independently". `blob.md:311-313`: a shallow clone "relies on continued source retention, such as a source tag, and does not independently extend object lifetime." `blob.md:305-307`: "Referencing another dataset's base does not grant deletion authority, and a missing source manifest does not prove descendant branches have released their references." PROVENANCE corrects the layout example on 14.0.3 (one base path, id 0, no name).

## 7. Files changed in place

- Data files: "UUID-based filenames" (`layout.md:193-201`); manifests are immutable ("Each version is described by an immutable manifest", `index.md:5`). No checksum or ETag of a data file is documented. `file_metadata_size_bytes` is "advisory … The footer stays authoritative. Readers must ignore a value larger than the file" (`index.md:194-206`).
- Deletion files with paths: "Each new deletion file is written to a new, unique path … so existing files are never overwritten." (`index.md:225-226`).
- Blobs: "External descriptors store no ETag, content hash, or object version to detect mutation. Owners should use immutable object locations when reproducible reads are required. Reference writes need not check existence or length" (`blob.md:154-158`). Managed: "Published Lance-owned payloads must remain immutable while protected snapshots reference them." (`:256-257`). "Non-empty reads of missing or truncated payloads must fail" (`:190-191`). "External objects are never deleted by Lance." (`:300`).
- Manifests under an external store: size validated, ETag "neither content identity nor dataset-incarnation identity" (`transaction.md:1024-1026`, `:1040`).
- External bases: nothing beyond the base-path resolution rules (`layout.md:183-220`); grep `etag|checksum|sha256|crc|content hash` over `lance/format/table` and the named guide pages finds only `blob.md:155` and `transaction.md` § External Manifest Store.
- **Measured:** a fragment's `DataFile` records `file_size_bytes` (1627). Replacing that file with another valid Lance file of 407 bytes → scan fails `Invalid range 0..1627 for object of size 407 bytes`. Flipping one byte at offset 100 of a same-size data file → scan succeeds and returns wrong values (sum 499752, expected 499500). Size is checked incidentally by the read range; same-size changes are not detected.

## 8. Governance topics

Greps were run over `lance/format/table/*.md`, `lance/guide/{tags_and_branches,read_and_write,object_store}.md` and `ns_catalog/` (excluding generated `operations/models/`), case-insensitive.

| Topic | grep | Finding |
| --- | --- | --- |
| Soft delete | `soft[- ]delet` | Rows: deletion files record "tombstones of soft-deleted rows" (`versioning.md` `2^0`). Tables: `spec.yaml:899`, `:5027` "`DeregisterTable`: Deregister (soft-delete) a table"; `DeregisterTableRequest` "The table content remains available in the storage." (`:4089`); dir V1 marker "the table can be re-registered by removing the marker file and calling `RegisterTable`" (`lance-dir.md:224`). |
| Protection | `protect` | Only snapshot protection from cleanup (section 6, `blob.md`). No protect-from-drop concept. |
| Undrop | `undrop|undelete|recycle|trash` | Nothing found. (Re-registering deregistered files, row above, is the nearest.) |
| Idempotency keys | `idempoten|idempotency|request[_ -]id` | No idempotency key in requests. "This operation is idempotent" for finalizing a staged manifest (`transaction.md:1031`, `lance-dir.md:377`); fragment-tree removal actions idempotent (`fragment_metadata.md:342`); `x-request-id` only as a context-header example (`spec.yaml:2489`, `lance-rest.md:17`). |
| Roles | `\brole\b|\broles\b|rbac` | Nothing found (only IAM Roles for Service Accounts, `object_store.md:336`). |
| Grants | `\bgrant` | Nothing found (`blob.md:305` "does not grant deletion authority"). DeclareTable "sets up aspects like access control" (`spec.yaml:1980`) and PermissionDenied/Unauthenticated codes 15/16 are the only authz surface. |
| Events | `event|notification|webhook` | Nothing found. |
| Lineage between tables | `lineage` | Nothing found; "lineage" is row-level (`row_id_lineage.md`: row ids, created/updated-at versions, change data feed `:426-454`). `__manifest.base_objects` "Reserved for future use (e.g., view dependencies)" (`dir/index.md:111`) with a LabelList index "for view dependency queries" (`:129`). |
| Data contracts | `data contract|contract` | Nothing found. The nearest: unenforced primary key "fixed after initial setting and must not be updated or removed" (`index.md:81-104`); `TableSchemaValidationError` (code 20). |
| Labels | `\blabel` (excluding LABEL_LIST) | Nothing found ("Tags label specific versions", `tags_and_branches.md:6`). |
| Retention policies | `retention` | Version retention only: cleanup windows, `retain_versions`, auto-cleanup config (`read_and_write.md:858-1111`, `table_config.md:32-36`). |
| Erasure | `erasure|gdpr|right to be forgotten|purge` | Nothing found. The only statement: removed versions "cannot be recovered" (`read_and_write.md:877-879`). |
| Audit | `audit` | `catalog/rest/index.md:11` "enforce governance policies such as auditing, access control, and commit validation" — as a purpose of the version APIs only. |

## 9. Multi-table atomicity

- Operations: `BatchCreateTableVersions` (`spec.yaml:838-878`, "either all table versions are created successfully, or none are created", `:850-852`) and `BatchCommitTables` (`:880-924`): DeclareTable, CreateTableVersion, DeleteTableVersions, DeregisterTable, "committed atomically: either all succeed or none are applied" (`:891-901`); `BatchCommitTablesRequest` "replaces `BatchCreateTableVersionsRequest`" (`:4988`). Both carry `transaction_id`.
- Partial success: `lance-dir.md:314` (management off) "Atomicity is not guaranteed"; `:324` (on) "If any version already exists, the entire batch fails". `partitioning-spec.md:302-305` "Writes across partitions are not atomic or consistent: A write that affects multiple partitions may partially succeed … Reads across partitions are not isolated"; stronger guarantees by recording `read_version`/`read_branch`/`read_tag` per table in "a single `__manifest` commit" (`:307-327`), with conflict resolution "Rebase … and retry … or Fail" (`:331-336`).
- Within one table: `CompositeOperation` applies many actions "atomically as one manifest change" (`transaction.md:644-649`).
- **Measured:** `DirectoryNamespace.batch_create_table_versions` and `batch_commit_tables` are not defined by the dir class; the base class raises `UnsupportedOperationError: Not supported: batch_create_table_versions` / `…batch_commit_tables`. The binary also carries `batch_create_table_versions not implemented` and `batch_commit_tables not implemented`. PROVENANCE: the bundled REST client leaves the same two to the base class. `LanceDataset.commit_batch` takes a single `dest`.

## 10. Planned, reserved, deprecated or changed

- `__manifest.base_objects`: "Reserved for future use (e.g., view dependencies)" (`dir/index.md:111`).
- `BatchCommitTablesRequest` "replaces `BatchCreateTableVersionsRequest`" (`spec.yaml:4988`); both still in the spec, neither implemented by the 14.0.3 dir backend.
- Commit point and ETag: the lance tree now puts the commit point at materialization and forbids persisting ETags (`transaction.md:982-998`, Rolling Upgrade `:1008-1014`); the ns_catalog tree (older pin) still says the `__manifest` row is the commit and stores `e_tag` (`lance-dir.md:298`, `dir/index.md:176`, `spec.yaml:5089-5093`).
- Managed versioning key: docs `table_version_management=true` in `__manifest` metadata; 14.0.3 has a constructor property `table_version_tracking_enabled` and writes no version rows (section 4c).
- `transaction_section_deprecated` (field 21) "New writers must leave it absent" (`transaction.md:47`).
- Transaction V2 `CompositeOperation`: "Readers before v14.0.0 fail on a `CompositeOperation`" … "Writers commit a `CompositeOperation` only when the calling code opts in per commit" (`transaction.md:630-642`).
- Operation versioning: an incompatible change makes `<OperationId>V<version>` (`namespace/operations/index.md:161-165`); the spec's `info.version` stays `1.0.0` across breaking changes (PROVENANCE).
- 15.0.0 (ahead of the lock): version-op Python return types change to models (`lance/guide/migration.md:13-21`).
- Error code 12: `errors.md:25` and `spec.yaml:2424` define 12 = `TableColumnNotFound`; `lance-dir.md:306`, `:332` and `lance-rest.md:510`, `:581` use "12 (TableVersionAlreadyExists)", and `lance-rest.md:1003` maps 12 to 404 while `:510` says 409. `lance_namespace` 0.13 has no `TableVersionAlreadyExistsError`; the 14.0.3 dir backend answers an existing version with `ConcurrentModificationError` (14).
- Experimental/unstable: data overlay files "experimental and not yet supported in any library" (`index.md:251-253`, flag `2^6` unstable); reserved flags 12, 20, 22 (`versioning.md` § Current Feature Flags, last paragraph); deprecated `enable_binary_copy` fields (`table_config.md:46`); `CleanupPolicy` docs-only on 14.0.3 (section 6).
- `lance/guide/object_store.md:410-421`: Lance "does not check whether the store enforces" `If-None-Match: *`; safe concurrent writes need it or a shared commit lock.

---

## Could not confirm

- Whether `table_version_tracking_enabled` is the 14.0.3 counterpart of `table_version_management` or a different feature: the binary string list was the only evidence; no source for 14.0.3 was read, and it wrote no `table_version` rows, so the documented `__manifest` external-store path was not observed at all.
- Why `list_all_tables` returned `[]` with tables present (with `dir_listing_enabled=false`): cause not investigated; measured twice with two request shapes.
- That pylance's namespace-managed commit is built on `LanceNamespaceExternalManifestStore`: inferred from binary symbol names only.
- Behaviour against S3/RustFS: every measurement was on a local filesystem; e_tags shown are local-fs ETags. Concurrency (two real writers racing) was not measured; the lost race was simulated by a namespace that raises.
- A tag or branch created while a cleanup runs on 14.0.3: not measured (PROVENANCE: last measured on 12/13).
- `DropTable` on the dir backend with data: not measured in a clean run (the one attempt failed earlier at `register_table`).
- Whether `alter_transaction`'s `_alter_transactions/<uuid>.json` file changes any later answer: only its creation was observed; the answered status stayed `SUCCEEDED`.
- Protobuf field lists for `Transaction`/`Manifest` (`transaction_properties`, author-like fields): the pages hold unrendered placeholders and pylance ships no `.proto`, so only what pylance returns was observed.
