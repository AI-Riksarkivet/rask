Directory structure:
└── src/
    ├── index.md
    ├── partitioning-spec.md
    ├── .pages
    ├── catalog/
    │   ├── index.md
    │   ├── .pages
    │   ├── dir/
    │   │   └── index.md
    │   └── rest/
    │       └── index.md
    └── namespace/
        ├── index.md
        ├── object-relationship.md
        ├── .pages
        ├── operations/
        │   ├── errors.md
        │   ├── index.md
        │   ├── .pages
        │   └── models/
        │       ├── AddColumnsEntry.md
        │       ├── AddVirtualColumnEntry.md
        │       ├── AddVirtualColumnOutputEntry.md
        │       ├── AlterColumnsEntry.md
        │       ├── AlterTableAddColumnsRequest.md
        │       ├── AlterTableAddColumnsResponse.md
        │       ├── AlterTableAlterColumnsRequest.md
        │       ├── AlterTableAlterColumnsResponse.md
        │       ├── AlterTableBackfillColumnsRequest.md
        │       ├── AlterTableBackfillColumnsResponse.md
        │       ├── AlterTableDropColumnsRequest.md
        │       ├── AlterTableDropColumnsResponse.md
        │       ├── AlterTransactionAction.md
        │       ├── AlterTransactionRequest.md
        │       ├── AlterTransactionResponse.md
        │       ├── AlterTransactionSetProperty.md
        │       ├── AlterTransactionSetStatus.md
        │       ├── AlterTransactionUnsetProperty.md
        │       ├── AlterVirtualColumnEntry.md
        │       ├── AnalyzeTableQueryPlanRequest.md
        │       ├── AnalyzeTableQueryPlanResponse.md
        │       ├── BatchCommitTablesRequest.md
        │       ├── BatchCommitTablesResponse.md
        │       ├── BatchCreateTableVersionsRequest.md
        │       ├── BatchCreateTableVersionsResponse.md
        │       ├── BatchDeleteTableVersionsRequest.md
        │       ├── BatchDeleteTableVersionsResponse.md
        │       ├── BooleanQuery.md
        │       ├── BoostQuery.md
        │       ├── BranchApi.md
        │       ├── BranchContents.md
        │       ├── CommitTableOperation.md
        │       ├── CommitTableResult.md
        │       ├── CountTableRowsRequest.md
        │       ├── CountTableRowsResponse.md
        │       ├── CreateMaterializedViewRequest.md
        │       ├── CreateMaterializedViewResponse.md
        │       ├── CreateNamespaceRequest.md
        │       ├── CreateNamespaceResponse.md
        │       ├── CreateTableBranchRequest.md
        │       ├── CreateTableBranchResponse.md
        │       ├── CreateTableIndexRequest.md
        │       ├── CreateTableIndexResponse.md
        │       ├── CreateTableRequest.md
        │       ├── CreateTableResponse.md
        │       ├── CreateTableScalarIndexResponse.md
        │       ├── CreateTableTagRequest.md
        │       ├── CreateTableTagResponse.md
        │       ├── CreateTableVersionEntry.md
        │       ├── CreateTableVersionRequest.md
        │       ├── CreateTableVersionResponse.md
        │       ├── DeclareTableRequest.md
        │       ├── DeclareTableResponse.md
        │       ├── DeleteFromTableRequest.md
        │       ├── DeleteFromTableResponse.md
        │       ├── DeleteTableBranchRequest.md
        │       ├── DeleteTableBranchResponse.md
        │       ├── DeleteTableTagRequest.md
        │       ├── DeleteTableTagResponse.md
        │       ├── DeregisterTableRequest.md
        │       ├── DeregisterTableResponse.md
        │       ├── DescribeNamespaceRequest.md
        │       ├── DescribeNamespaceResponse.md
        │       ├── DescribeTableIndexStatsRequest.md
        │       ├── DescribeTableIndexStatsResponse.md
        │       ├── DescribeTableRequest.md
        │       ├── DescribeTableResponse.md
        │       ├── DescribeTableVersionRequest.md
        │       ├── DescribeTableVersionResponse.md
        │       ├── DescribeTransactionRequest.md
        │       ├── DescribeTransactionResponse.md
        │       ├── DropNamespaceRequest.md
        │       ├── DropNamespaceResponse.md
        │       ├── DropTableIndexRequest.md
        │       ├── DropTableIndexResponse.md
        │       ├── DropTableRequest.md
        │       ├── DropTableResponse.md
        │       ├── ErrorResponse.md
        │       ├── ExplainTableQueryPlanRequest.md
        │       ├── ExplainTableQueryPlanResponse.md
        │       ├── FragmentStats.md
        │       ├── FragmentSummary.md
        │       ├── FtsQuery.md
        │       ├── GetTableStatsRequest.md
        │       ├── GetTableStatsResponse.md
        │       ├── GetTableTagVersionRequest.md
        │       ├── GetTableTagVersionResponse.md
        │       ├── Identity.md
        │       ├── IndexContent.md
        │       ├── InsertIntoTableRequest.md
        │       ├── InsertIntoTableResponse.md
        │       ├── JsonArrowDataType.md
        │       ├── JsonArrowField.md
        │       ├── JsonArrowSchema.md
        │       ├── ListNamespacesRequest.md
        │       ├── ListNamespacesResponse.md
        │       ├── ListTableBranchesRequest.md
        │       ├── ListTableBranchesResponse.md
        │       ├── ListTableIndicesRequest.md
        │       ├── ListTableIndicesResponse.md
        │       ├── ListTablesRequest.md
        │       ├── ListTablesResponse.md
        │       ├── ListTableTagsRequest.md
        │       ├── ListTableTagsResponse.md
        │       ├── ListTableVersionsRequest.md
        │       ├── ListTableVersionsResponse.md
        │       ├── MatchQuery.md
        │       ├── MaterializedViewApi.md
        │       ├── MaterializedViewUdtfEntry.md
        │       ├── MergeInsertIntoTableRequest.md
        │       ├── MergeInsertIntoTableResponse.md
        │       ├── MultiMatchQuery.md
        │       ├── NamespaceExistsRequest.md
        │       ├── NamespaceExistsResponse.md
        │       ├── PartitionField.md
        │       ├── PartitionSpec.md
        │       ├── PartitionTransform.md
        │       ├── PhraseQuery.md
        │       ├── QueryTableRequest.md
        │       ├── QueryTableRequestColumns.md
        │       ├── QueryTableRequestFullTextQuery.md
        │       ├── QueryTableRequestVector.md
        │       ├── QueryTableResponse.md
        │       ├── RefreshMaterializedViewRequest.md
        │       ├── RefreshMaterializedViewResponse.md
        │       ├── RegisterTableRequest.md
        │       ├── RegisterTableResponse.md
        │       ├── RenameTableRequest.md
        │       ├── RenameTableResponse.md
        │       ├── RestoreTableRequest.md
        │       ├── RestoreTableResponse.md
        │       ├── StringFtsQuery.md
        │       ├── StructuredFtsQuery.md
        │       ├── TableBasicStats.md
        │       ├── TableExistsRequest.md
        │       ├── TableExistsResponse.md
        │       ├── TableVersion.md
        │       ├── TagContents.md
        │       ├── UpdateFieldMetadataEntry.md
        │       ├── UpdateFieldMetadataRequest.md
        │       ├── UpdateFieldMetadataResponse.md
        │       ├── UpdateTableRequest.md
        │       ├── UpdateTableResponse.md
        │       ├── UpdateTableSchemaMetadataRequest.md
        │       ├── UpdateTableSchemaMetadataResponse.md
        │       ├── UpdateTableTagRequest.md
        │       ├── UpdateTableTagResponse.md
        │       ├── VersionRange.md
        │       └── .pages
        └── supported-catalogs/
            ├── index.md
            ├── lance-dir.md
            ├── lance-rest.md
            ├── template.md
            └── .pages


Files Content:

================================================
FILE: docs/src/index.md
================================================
# Lance Catalog & Namespace Specs

This is the local development index page for the Lance Catalog & Namespace specs.

In production, the [Catalog Specs](catalog/index.md) and [Namespace Client Spec](namespace/index.md) documentation are copied separately to the main Lance documentation site.



================================================
FILE: docs/src/partitioning-spec.md
================================================
# Lance Partitioning Spec

Partitioning is a common data organization strategy that divides data into physically separated units.
Lance tables do not natively support partitioning, instead promoting clustering to achieve similar performance benefits.

However, there are use cases where true partitioning makes sense.
For example, an organization might want to store one table per business unit, 
where each table is fully isolated yet shares a common schema and data management lifecycle.
Most of the time, queries like vector search are only against a specific partition, but sometimes 
it would be convenient to query across all business units as a unified dataset.

A **Partitioned Namespace** is designed for these use cases.
It is a [Directory Catalog](catalog/dir/index.md) containing a collection of tables that share a common schema.
These tables are physically separated and independent, but logically related through partition fields definition.

This document defines the storage format for Partitioned Namespace.
Similar to Lance being a storage-only format, the storage-only [Directory Catalog](catalog/dir/index.md) spec serves as the foundation for this Partitioned Namespace format.

The following example illustrates the logical layout of a partitioned namespace:

```text
Root Namespace (__manifest Lance table)
┌──────────────────────────────────────────────────┐
│ Table metadata (root namespace properties):      │
│     - schema = <shared Schema>                   │
│     - partition_spec_v1 = [event_date]           │
│     - partition_spec_v2 = [event_year, country]  │
└──────────────────────────────────────────────────┘
                        │
                Spec Version Level
                        │
        ┬───────────────┴───────────────┐
        │                               │
       v1                              v2
    (Namespace)                     (Namespace)
        │                               │
        │── <id1>                       │── <id3>
        │   (Namespace)                 │   (Namespace)
        │   event_date=2025-12-10       │   event_year=2025
        │     └── dataset (Table)       │     │
        │                               │     └── <id4>
        │── <id2>                       │         (Namespace)
        │   (Namespace)                 │         country=US
        │   event_date=2025-12-11       │           └── dataset (Table)
        │     └── dataset (Table)       │
        └── ...                         └── ...
```

## Metadata Definition

A directory catalog is identified as a partitioned namespace if the `__manifest` table's
[metadata](catalog/dir/index.md#root-namespace-properties) contains at least one partition spec version key.

The following properties are stored in the `__manifest` table's metadata map:

- `partition_spec_v<N>` (String): A JSON string representing a partition spec object for version N. The object contains the spec ID and an array of partition field definitions. See [Partition Spec](#partition-spec) for details.
- `schema` (String): A json string describing the Schema of the entire partitioned namespace, based on the [JsonArrowSchema](namespace/operations/models/JsonArrowSchema.md) schema in the Namespace Client spec. See [Namespace Schema](#schema) for more details.

See [Appendix A: Metadata Example](#appendix-a-metadata-example) for a complete example.

## Schema

The **Namespace Schema** defines the schema for all partition tables in the partitioned namespace.
Implementations must enforce that **all partition table schemas must be consistent with each other, as well as with the namespace schema**.
Most importantly, each field in the schema has a unique field ID stored in metadata under the key `lance:field_id`.
Field IDs are never reused and must remain consistent across partition tables.
This ensures partition specs using `source_ids` remain valid even if columns are renamed.

## Partition Spec

The **Namespace Partition Spec** defines how to derive partition values from a record in a partitioned namespace.
The partitioning information is stored in `partition_spec_v<N>` (e.g., `partition_spec_v1`),
which is a JSON object containing a spec ID and an array of partition field definitions.

### Partition Spec Schema

A partition spec is a JSON object with the following fields:

| Field        | JSON representation     | Example | Description                                                                                  |
|--------------|-------------------------|---------|----------------------------------------------------------------------------------------------|
| **`id`**     | `JSON int`              | `1`     | The spec version ID, matching the `N` in the key name                                        |
| **`fields`** | `JSON array of objects` | `[...]` | Array of partition field definitions (see [Partition Field Schema](#partition-field-schema)) |

### Partition Field Schema

Each element in the `fields` array is a partition field object with the following fields:

| Field             | JSON representation | Example                     | Description                                                                                                                                     |
|-------------------|---------------------|-----------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------|
| **`field_id`**    | `JSON string`       | `"event_year"`              | Unique identifier for this partition field (must not be renamed)                                                                                |
| **`source_ids`**  | `JSON int array`    | `[1]`                       | Field IDs of the source columns in the schema                                                                                                   |
| **`transform`**   | `JSON object`       | `{ "type": "year" }`        | Well-known partition transform (see [Partition Transform](#partition-transform)). Exactly one of `transform` or `expression` must be specified. |
| **`expression`**  | `JSON string`       | `"date_part('year', col0)"` | DataFusion SQL expression using `col0`, `col1`, ... as column references. Exactly one of `transform` or `expression` must be specified.         |
| **`result_type`** | `JSON object`       | `{ "type": "int32" }`       | The output type of the partition value ([JsonArrowDataType](namespace/operations/models/JsonArrowDataType.md) format)                              |

**Transform vs Expression**: Exactly one of `transform` or `expression` must be specified. When `transform` is specified, the expression is derived from the transform type. Custom partition logic that doesn't fit a well-known transform must use `expression` directly.

**Partition Field ID**: The `field_id` is a string that uniquely identifies each partition field across all spec versions. It is used as the column name suffix in `__manifest` (e.g., `partition_field_event_year`). Once assigned, a `field_id` must never be renamed or reused. This ensures stable column names in the manifest table.

**Field ID Reuse**: When evolving partition specs, if a new partition field has the same `source_ids` and `transform` (or `expression`) as an existing field, the same `field_id` must be reused. Otherwise, a new unique `field_id` must be assigned.

**Source Field IDs**: The `source_ids` array references field IDs stored in the schema's field metadata under the key `lance:field_id`. Using field IDs instead of column names ensures that partition specs remain valid even when source columns are renamed. In the partition expression, source columns are referenced as `col0`, `col1`, `col2`, etc., corresponding to the order of field IDs in the `source_ids` array.

### Partition Expression

The `expression` field contains a [DataFusion SQL expression](https://datafusion.apache.org/user-guide/sql/index.html) that transforms source column values into a partition value.
The placeholders `col0`, `col1`, `col2`, etc. represent the source columns in order corresponding to the `source_ids` array.
For single-column partitions, only `col0` is used.
The expression result type is declared by the `result_type` field.

All partition expressions must satisfy the following requirements:

1. **Deterministic**: The same input value must always produce the same output value.
2. **Stateless**: The expression must not depend on external state (e.g., current time, random values, session variables).
3. **Type-promotion resistant**: The expression must produce the same result for equivalent values regardless of their numeric type (e.g., `int32(5)` and `int64(5)` must yield the same partition value).
4. **Column removal resistant**: If a source field ID is not found in the schema, the column should be interpreted as NULL.
5. **NULL safe**: The partition expression should properly handle NULL case and have defined behavior (e.g. return NULL if NULL for single-column expression, ignore the NULL column for multi-column expression)
6. **Consistent with result type**: The `result_type` field declares the output type of the partition expression as an Arrow data type.
  This enables type checking without expression evaluation and ensures consistency across implementations.
  The partition expression's return type must be consistent with the result type in non-NULL case.

### Partition Transform

Partition transforms are **well-known partition expressions** with structured metadata that enables query optimization such as [Storage Partitioned Join](#storage-partitioned-join). 
When a partition field uses a well-known transform, the `transform` field should be specified instead of the `expression` field.

#### Transform Schema

The `transform` field is a JSON object with the following structure:

| Field             | JSON representation | Required                | Description                          |
|-------------------|---------------------|-------------------------|--------------------------------------|
| **`type`**        | `JSON string`       | Yes                     | The transform type (see table below) |
| **`num_buckets`** | `JSON int`          | For bucket transforms   | Number of buckets N                  |
| **`width`**       | `JSON int`          | For truncate transforms | Truncation width W                   |

#### Supported Transforms

| Transform Type | Parameters    | Derived Expression                                        | Result Type    | Description                              |
|----------------|---------------|-----------------------------------------------------------|----------------|------------------------------------------|
| `identity`     | (none)        | `col0`                                                    | same as source | Source value, unmodified                 |
| `year`         | (none)        | `date_part('year', col0)`                                 | `int32`        | Extract year from date/timestamp         |
| `month`        | (none)        | `date_part('month', col0)`                                | `int32`        | Extract month (1-12) from date/timestamp |
| `day`          | (none)        | `date_part('day', col0)`                                  | `int32`        | Extract day of month from date/timestamp |
| `hour`         | (none)        | `date_part('hour', col0)`                                 | `int32`        | Extract hour (0-23) from timestamp       |
| `bucket`       | `num_buckets` | `abs(murmur3(col0)) % N`                                  | `int32`        | Hash single column into N buckets        |
| `multi_bucket` | `num_buckets` | `abs(murmur3_multi(col0, col1, ...)) % N`                 | `int32`        | Hash multiple columns into N buckets     |
| `truncate`     | `width`       | `left(col0, W)` (string) or `col0 - (col0 % W)` (numeric) | same as source | Truncate to width W                      |

#### Hash Functions

The `bucket` and `multi_bucket` transforms use Murmur3 hash functions provided as Lance extensions to DataFusion:

- **`murmur3(col)`**: Computes the 32-bit Murmur3 hash (x86 variant, seed 0) of a single column. Returns a signed 32-bit integer. Returns NULL if input is NULL.
- **`murmur3_multi(col0, col1, ...)`**: Computes the Murmur3 hash across multiple columns. Returns a signed 32-bit integer. NULL fields are ignored during hashing; returns NULL only if all inputs are NULL.

The hash result is wrapped with `abs()` and modulo `N` to produce a non-negative bucket number in the range `[0, N)`.
For implementations that do not use DataFusion, the same behavior for hashing should be preserved.

## Physical Layout and Naming

A partitioned namespace supports multi-level partitioning with the following physical hierarchy:

- **Root Namespace**: The root namespace is implicit and represented by the `__manifest` table itself. Its properties (partition specs, schema) are stored in the `__manifest` table's metadata.
- **Spec Version Namespace**: The first-level child namespace, named `v1`, `v2`, etc. This identifies which partition spec version the data underneath was written with. When retrieving properties via API, these namespaces dynamically include a `partition_spec` property containing the partition spec for that version (copied from the root's `partition_spec_v<N>`).
- **Partition Namespace**: Each subsequent level of child namespaces represents a partition field. The order of partition namespace levels corresponds to the order of partition fields in the partition spec. Namespace names are randomly generated identifiers (see [Namespace Naming](#partition-namespace-naming)).
- **Partition Table**: At the end of the partition hierarchy, a `Table` object with the fixed name `dataset` contains the actual data. This is a standard, independently accessible Lance `Dataset` containing a subset of the partitioned namespace's data.

See [Appendix B: Physical Layout Example](#appendix-b-physical-layout-example) for a complete directory structure example.

### Partition Namespace Naming

Partition namespaces use **random identifier naming** to avoid issues with special characters in partition values.

Partition namespace names are randomly generated 16-character base36 strings (using characters `a-z0-9`).
This provides ~83 bits of entropy, ensuring virtually zero collision probability for any practical number of partitions.
This approach ensures:

- No conflicts with reserved characters (e.g., `$`, `/`, `=`) that may appear in partition column values
- Consistent namespace names across different client implementations
- Fixed-length, predictable namespace identifiers

Since namespace names are random identifiers,
the actual partition values are stored in the `__manifest` table's partition columns (see [Manifest Table Schema](#manifest-table-schema)).

### Runtime Namespace Properties

Since namespace names are random identifiers, the actual partition values are stored in the
`__manifest` table's partition columns (see [Manifest Table Schema](#manifest-table-schema)).

Implementations may dynamically populate properties when retrieving namespace information via API:

- For partition namespaces: `partition.<field_id> = <value>` entries
- For spec version namespaces (v1, v2, etc.): `partition_spec` containing the partition spec for that version

These runtime properties are optional. Implementations may choose not to expose them for security or other reasons.
See [Appendix E: Runtime Namespace Properties Example](#appendix-e-runtime-namespace-properties-example) for examples.

## Query Optimization

This section describes query optimization techniques that leverage partitioned namespace metadata.

### Manifest Table Schema

The `__manifest` table schema is extended to include partition columns for efficient query optimization use cases. 
Instead of parsing namespace names to filter partitions, query engines can directly push down predicates to the manifest table.

**Extended Schema**: For each partition field defined in any partition spec version, 
the `__manifest` table includes an additional nullable column. 
The column name is `partition_field_{i}` where `{i}` is the partition field's `field_id`, and the type is the partition field's `result_type`. 
This naming convention avoids potential conflicts with user-defined column names. 
When a new partition spec version is defined, the `__manifest` table schema is updated accordingly to include any new partition columns.

| Column                       | Type     | Description                                                                 |
|------------------------------|----------|-----------------------------------------------------------------------------|
| `object_id`                  | `string` | Full namespace path with `$` separator (existing)                           |
| `object_type`                | `string` | `"namespace"` or `"table"` (existing)                                       |
| `metadata`                   | `string` | JSON-encoded metadata/properties (existing)                                 |
| `read_version`               | `uint64` | Table version for reads (optional, see [Transaction](#transaction))         |
| `read_branch`                | `string` | Table branch for reads (optional, see [Transaction](#transaction))          |
| `read_tag`                   | `string` | Table tag for reads (optional, see [Transaction](#transaction))             |
| `partition_field_{field_id}` | `<type>` | Partition value for the field (nullable, inherited from parent namespaces)  |
| ...                          | ...      | Additional partition field columns as needed                                |

Partition values are inherited from parent namespaces - each row has all partition values from its ancestors. 
See [Appendix C: Manifest Table Example](#appendix-c-manifest-table-example) for a complete example.

### Partition Pruning

Partition pruning is performed via the `__manifest` table, which contains partition column values for efficient filtering.

Here is the end-to-end workflow:

1. Query engine analyzes the query predicate to identify filters on partition columns
2. For each partition expression, the engine evaluates the expression with the query values to compute the expected partition value(s)
3. Engine queries `__manifest` with filters on the partition columns
4. Engine retrieves the paths of matching `dataset` tables
5. Engine scans only the relevant partition tables

### Storage Partitioned Join

Storage Partitioned Join (SPJ) is an optimization that eliminates or reduces shuffle operations when 
joining two partitioned datasets on their partition columns. 
When both sides of a join are partitioned by the same or compatible transforms on the join keys, 
the query engine can join partitions directly without redistributing data.

SPJ can be applied when:

1. Both datasets are partitioned by the same column(s) used in the join predicate
2. The partition transforms are compatible (see [Transform Compatibility](#transform-compatibility))
3. The query engine supports reporting partition information

For SPJ to work, the partition transforms must be compatible:

- **Same transform type**: Both sides use the same transform (e.g., both use `year` on a date column)
- **Bucket divisibility**: For bucket transforms, one bucket count must evenly divide the other. The side with fewer buckets becomes the "coarser" partition that may match multiple finer partitions.
- **Time hierarchy**: Coarser time transforms can match finer ones (e.g., `day` partitions can be grouped to match `month` partitions)

Here is the end-to-end workflow:

1. Query engine analyzes the join predicate to identify join keys
2. For each partitioned namespace, the engine reads the partition spec to determine the transform on join keys
3. If transforms are compatible, the engine computes which partitions can be joined without shuffle:
    - For identical transforms: Partitions with equal partition values are joined directly
    - For compatible bucket transforms: Partitions from the coarser side match multiple partitions from the finer side based on `finer_bucket % coarser_bucket_count`
    - For compatible time transforms: Partitions from the finer side are grouped to match coarser partitions
4. Engine executes the join partition-by-partition, avoiding full data shuffle

See [Appendix F: Storage Partitioned Join Example](#appendix-f-storage-partitioned-join-example) for a complete example.

## Partition Evolution

The partition spec supports **versioning** to allow partition strategies to evolve over time. 
Each partition spec version defines its own set of partition columns and expressions. 
Data written to the partitioned namespace records which spec version it was created under via the version namespace (`v1/`, `v2/`, etc.).

### Evolution Scenarios

- **Adding partition columns**: Create a new spec version with additional partition columns. New data is written under the new version while existing partitions remain accessible.
- **Changing partition expressions**: Create a new spec version with different expressions (e.g., changing from daily to yearly partitioning). Both versions coexist.
- **Removing partition columns**: Create a new spec version without certain columns. Legacy data under old versions remains queryable.

### Compatibility with Partition Pruning

When querying across multiple spec versions, the query engine must handle each version according to its partition spec. 
For example, if `v1` partitions by `event_date` and `v2` partitions by `year(event_date)`, a query filtering on `event_date = '2025-12-10'` will:

1. Match exact partitions in `v1`
2. Compute `year('2025-12-10') = 2025` and scan all matching year partitions in `v2`

This design ensures backward compatibility while enabling partition strategy evolution without data migration.

## Transaction

### Single-Partition Transaction

Operations within a single partition table are ACID-compliant according to the Lance table specification.
Each partition is an independent Lance table, so reads and writes to a single partition follow standard Lance transaction semantics.

### Multi-Partition Transaction

By default, operations across multiple partitions have weaker guarantees:

- **Writes across partitions are not atomic or consistent**: A write that affects multiple partitions may partially succeed, leaving some partitions updated while others are not.
- **Reads across partitions are not isolated**: A read spanning multiple partitions may observe different versions of each partition, leading to inconsistent views.

To enable stronger transactional guarantees across partitions, the `__manifest` table can optionally include `read_version`, `read_branch`, and `read_tag` columns for a table.
These columns record which version of each partition table to read.

#### Read Behavior

Users should specify one of the following combinations:

1. **`read_version` only**: Read the specified version from the main branch.
2. **`read_branch` + `read_version`**: Read the specified version from the specified branch.
3. **`read_tag` only**: Read the version referenced by the specified tag.

When all columns are NULL or not present, readers should read the latest version from the main branch.

#### Commit Behavior

Multi-partition transactions are guarded by commits against the `__manifest` table. A typical multi-partition write follows this pattern:

1. Write data to each affected partition table independently
2. Atomically update the `read_version` (and optionally `read_branch` or `read_tag`) of all affected partitions in a single `__manifest` commit

This ensures all-or-nothing visibility of changes across partitions.

#### Conflict Resolution

If concurrent commits have been committed to `__manifest` since the transaction began, the implementation must either:

1. Rebase the current commit onto the latest `__manifest` version and retry the commit, or
2. Fail the current commit and return an error to the caller

Implementations are responsible for ensuring the appropriate conflict detection and resolution strategy to guarantee ACID semantics during multi-partition transactions.

## Appendices

### Appendix A: Metadata Example

A complete example of partitioned namespace metadata properties with two spec versions:

```json
{
  "partition_spec_v1": {
    "id": 1,
    "fields": [
      {
        "field_id": "event_date",
        "source_ids": [1],
        "transform": { "type": "identity" },
        "result_type": { "type": "date32" }
      }
    ]
  },
  "partition_spec_v2": {
    "id": 2,
    "fields": [
      {
        "field_id": "event_year",
        "source_ids": [1],
        "transform": { "type": "year" },
        "result_type": { "type": "int32" }
      },
      {
        "field_id": "country",
        "source_ids": [2],
        "transform": { "type": "identity" },
        "result_type": { "type": "utf8" }
      }
    ]
  },
  "schema": {
    "fields": [
      {
        "name": "id",
        "nullable": false,
        "type": { "type": "int64" },
        "metadata": { "lance:field_id": "0" }
      },
      {
        "name": "event_date",
        "nullable": true,
        "type": { "type": "date32" },
        "metadata": { "lance:field_id": "1" }
      },
      {
        "name": "country",
        "nullable": true,
        "type": { "type": "utf8" },
        "metadata": { "lance:field_id": "2" }
      }
    ]
  }
}
```

In this example:
- `v1` partitions by `event_date` using the identity transform with `result_type: date32`
- `v2` partitions first by year of `event_date` using the year transform with `result_type: int32`, then by `country` using the identity transform with `result_type: utf8`
- The `__manifest` table will have three partition columns: `partition_field_event_date` (date32), `partition_field_event_year` (int32), `partition_field_country` (utf8)
- The schema follows [JsonArrowSchema](namespace/operations/models/JsonArrowSchema.md) format

### Appendix B: Physical Layout Example

A partitioned namespace with two spec versions (`v1` partitioned by `event_date`, `v2` partitioned by `event_year` and `country`) in [V2 Manifest](https://lance.org/format/namespace/dir/catalog-spec/#v2-manifest):

Namespaces exist only as entries in the `__manifest` table - they do not have physical directories. Only tables (the leaf `dataset` objects) have directories, following the V2 format `<hash>_<object_id>`.

```text
.
└── /my/dir1/
    ├── __manifest/                                                 # The manifest table
    │   ├── data/
    │   │   └── ...
    │   └── _versions/
    │       └── ...
    ├── b4a3c2d1_v1$k7m2n9p4q8r5s3t6$dataset/                       # Table: event_date=2025-12-10
    │   └── ...
    ├── 55667788_v1$w1x2y3z4a5b6c7d8$dataset/                       # Table: event_date=2025-12-11
    │   └── ...
    ├── aabbccdd_v2$e9f0g1h2i3j4k5l6$m7n8o9p0q1r2s3t4$dataset/      # Table: event_year=2025, country=US
    │   └── ...
    └── ...
```

The namespaces (`v1`, `v1$k7m2n9p4q8r5s3t6`, etc.) are tracked in the `__manifest` table but have no corresponding directories.

### Appendix C: Manifest Table Example

The `__manifest` table for a partitioned namespace with partition fields `event_date` (v1), `event_year` (v2) and `country` (v2), showing entries from both spec versions:

| object_id                                     | object_type | metadata | read_version | read_branch | read_tag | partition_field_event_date | partition_field_event_year | partition_field_country |
|-----------------------------------------------|-------------|----------|--------------|-------------|----------|----------------------------|----------------------------|-------------------------|
| v1                                            | namespace   | {}       | NULL         | NULL        | NULL     | NULL                       | NULL                       | NULL                    |
| v1$k7m2n9p4q8r5s3t6                           | namespace   | {}       | NULL         | NULL        | NULL     | 2025-12-10                 | NULL                       | NULL                    |
| v1$k7m2n9p4q8r5s3t6$dataset                   | table       | {}       | 5            | NULL        | NULL     | 2025-12-10                 | NULL                       | NULL                    |
| v2                                            | namespace   | {}       | NULL         | NULL        | NULL     | NULL                       | NULL                       | NULL                    |
| v2$e9f0g1h2i3j4k5l6                           | namespace   | {}       | NULL         | NULL        | NULL     | NULL                       | 2025                       | NULL                    |
| v2$e9f0g1h2i3j4k5l6$m7n8o9p0q1r2s3t4          | namespace   | {}       | NULL         | NULL        | NULL     | NULL                       | 2025                       | US                      |
| v2$e9f0g1h2i3j4k5l6$m7n8o9p0q1r2s3t4$dataset  | table       | {}       | 3            | NULL        | NULL     | NULL                       | 2025                       | US                      |

Note: The root namespace properties (`partition_spec_v1`, `partition_spec_v2`, `schema`) are stored in the `__manifest` table's metadata, not as a row. The `object_id` uses `$` as the namespace path separator. Partition columns use the naming convention `partition_field_{field_id}` where `{field_id}` is the partition field's string identifier. Partition values are inherited from parent namespaces. When retrieving properties via API, partition values are converted to `partition.<field_id> = <value>` entries.

See [Appendix D: Partition Pruning Example](#appendix-d-partition-pruning-example) for an example of how partition pruning queries work.

### Appendix D: Partition Pruning Example

This example demonstrates how a query engine translates a user query into a partition pruning query against the `__manifest` table.

Given a user query:

```sql
SELECT * FROM partitioned_namespace
WHERE event_date = '2025-12-10' AND country = 'US'
```

The engine translates this to the following `__manifest` DataFusion query plan to examine related partition tables.

```sql
SELECT object_id, location, read_version, read_branch, read_tag
FROM __manifest
WHERE object_type = 'table'
  AND (
    (object_id LIKE 'v1$%'
      AND partition_field_event_date = DATE '2025-12-10')
    OR
    (object_id LIKE 'v2$%'
      AND partition_field_event_year = date_part('year', DATE '2025-12-10')
      AND partition_field_country = 'US')
  )
```
Notice here that the query plan can leverage the partition expression, in this case `date_part('year', col0)`.
One example way to perform such substitution is:

1. Parsing the expression string (e.g., `date_part('year', col0)`) into an expression AST using DataFusion's SQL parser
2. Traversing the AST and replacing all `col0`, `col1`, etc. column references with the corresponding literal query values (e.g., `DATE '2025-12-10'`)
3. Evaluating the modified expression to produce the partition filter value (e.g., `2025`)

This query returns:

| object_id                                    | location                                              | read_version | read_branch | read_tag |
|----------------------------------------------|-------------------------------------------------------|--------------|-------------|----------|
| v1$k7m2n9p4q8r5s3t6$dataset                  | b4a3c2d1_v1$k7m2n9p4q8r5s3t6$dataset                  | 5            | NULL        | NULL     |
| v2$e9f0g1h2i3j4k5l6$m7n8o9p0q1r2s3t4$dataset | aabbccdd_v2$e9f0g1h2i3j4k5l6$m7n8o9p0q1r2s3t4$dataset | 3            | NULL        | NULL     |

- For partition spec v1, the `country = 'US'` filter cannot be pushed to partition pruning (v1 has no `country` partition), so it must be applied during the table scan
- For partition spec v2, both filters are pushed down: `partition_field_event_year = 2025` (computed from `year(event_date)`) and `partition_field_country = 'US'`
- The engine reads each table at the version specified by `read_version`, `read_branch`, or `read_tag` for consistent snapshot reads

### Appendix E: Runtime Namespace Properties Example

This appendix shows examples of runtime properties that implementations MAY return when describing namespaces.
These are optional behaviors - implementations may choose not to expose them for security or other reasons.

**Spec Version Namespace**

`DescribeNamespace(["v1"])` returns:

```json
{
  "properties": {
    "partition_spec": "{\"id\":1,\"fields\":[{\"field_id\":\"event_date\",\"source_ids\":[1],\"transform\":{\"type\":\"identity\"},\"result_type\":{\"type\":\"date32\"}}]}"
  }
}
```

**Partition Namespace (v1)**

`DescribeNamespace(["v1", "k7m2n9p4q8r5s3t6"])` returns:

```json
{
  "properties": {
    "partition.event_date": "2025-12-10"
  }
}
```

**Partition Namespace (v2, first level)**

`DescribeNamespace(["v2", "e9f0g1h2i3j4k5l6"])` returns:

```json
{
  "properties": {
    "partition.event_year": "2025"
  }
}
```

**Partition Namespace (v2, second level)**

`DescribeNamespace(["v2", "e9f0g1h2i3j4k5l6", "m7n8o9p0q1r2s3t4"])` returns:

```json
{
  "properties": {
    "partition.country": "US"
  }
}
```

Note: Each namespace only returns the partition value for its own level.
To get all partition values in a path, the client must query each ancestor namespace.

### Appendix F: Storage Partitioned Join Example

This example demonstrates how a query engine performs a Storage Partitioned Join (SPJ) between two partitioned namespaces.

**Setup**: Two partitioned namespaces with compatible bucket transforms:

- `orders` namespace: partitioned by `bucket(customer_id, 16)` with partition field `customer_bucket`
- `customers` namespace: partitioned by `bucket(id, 8)` with partition field `id_bucket`

**User Query**:

```sql
SELECT o.*, c.name
FROM orders o
JOIN customers c ON o.customer_id = c.id
```

**SPJ Analysis**:

1. The engine reads partition specs from both namespaces' `__manifest` tables
2. Both join keys use bucket transforms: `orders.customer_id` → `bucket(16)`, `customers.id` → `bucket(8)`
3. Since 8 divides 16 evenly, the transforms are compatible

**Partition Matching**:

For each `customers` partition with bucket value `i`, 
the matching `orders` partitions have bucket values where `bucket % 8 == i`:

| customers bucket | orders buckets |
|------------------|----------------|
| 0                | 0, 8           |
| 1                | 1, 9           |
| 2                | 2, 10          |
| 3                | 3, 11          |
| 4                | 4, 12          |
| 5                | 5, 13          |
| 6                | 6, 14          |
| 7                | 7, 15          |

**Execution Plan**:

The engine queries both `__manifest` tables to get partition locations:

```sql
-- Get orders partitions
SELECT partition_field_customer_bucket, location, read_version
FROM orders.__manifest
WHERE object_type = 'table'

-- Get customers partitions
SELECT partition_field_id_bucket, location, read_version
FROM customers.__manifest
WHERE object_type = 'table'
```

For each customers partition `i`, the engine:

1. Reads the customers partition where `partition_field_id_bucket = i`
2. Reads the orders partitions where `partition_field_customer_bucket % 8 = i`
3. Performs a local join without shuffle

**Result**: The join completes with 8 parallel partition-wise joins instead of a full shuffle of both datasets.



================================================
FILE: docs/src/.pages
================================================
nav:
  - index.md
  - Catalog Specs: catalog
  - Namespace Client Spec: namespace



================================================
FILE: docs/src/catalog/index.md
================================================
# Lance Catalog Specs

A **catalog** manages collections of tables and provides table discovery, management, and transactional coordination. Catalog implementations vary widely across deployments, ranging from lightweight environments to enterprise platforms integrating with authorization systems or metadata services such as Apache Hive metastores.

To support this range of environments, Lance provides two catalog approaches:

## Directory Catalog

The **[Directory Catalog](dir/index.md)** is a storage-native catalog format that requires only a filesystem or object store — no additional services are needed. This makes it suitable for lightweight deployments, or even embedded in-process databases.

Key characteristics:

- **Zero infrastructure**: Requires only storage (local filesystem, S3, GCS, Azure, etc.)
- **Transactional guarantees**: Catalog metadata is stored as a Lance table, inheriting transactional semantics, snapshot isolation, and schema evolution guarantees
- **Simple deployment**: Ideal for ML/AI workloads that favor minimal operational dependencies

## REST Catalog

The **[REST Catalog](rest/index.md)** is an OpenAPI-based protocol that enables reading, writing, and managing Lance tables through a REST API. This is ideal for enterprise environments that require integration with existing governance, access control, and compliance systems.

Key characteristics:

- **Enterprise integration**: Connect to existing metadata services and authorization systems
- **Standardized API**: OpenAPI specification enables consistent client/server implementations
- **External manifest store**: Table version management APIs can act as an external manifest store for governance policies

## Supported Catalogs

Beyond the natively maintained catalog specs, Lance supports integration with external catalog systems through the [Namespace Client Spec](../namespace/index.md). Namespace Client implementation specs for systems like Apache Polaris, Unity Catalog, Apache Hive Metastore, and Apache Iceberg REST Catalog are maintained separately and can be found in the [Supported Catalogs](../namespace/supported-catalogs/index.md) section.



================================================
FILE: docs/src/catalog/.pages
================================================
title: Catalog Specs
nav:
  - Overview: index.md
  - Directory Catalog: dir
  - REST Catalog: rest



================================================
FILE: docs/src/catalog/dir/index.md
================================================
# Lance Directory Catalog

The **Lance Directory Catalog** is a storage-native catalog format that stores tables in a directory structure on any local or remote storage system. It requires no external metadata service — only a filesystem or object store.

Machine learning workloads frequently operate on datasets stored in object storage and favor minimal operational dependencies, even in production environments. However, existing lakehouse formats typically require an external catalog service, while storage-only approaches lack the transactional guarantees required for reliable production use. The Directory Catalog addresses this gap by providing a catalog built directly on top of the Lance table format.

The Directory Catalog has gone through 2 major spec versions:

- **V1 (Directory Listing)**: A lightweight, simple 1-level namespace that discovers tables by scanning the directory.
- **V2 (Manifest)**: A more advanced implementation backed by a manifest table (a Lance table) that supports nested namespaces and better performance at scale.

## V1: Directory Listing

V1 is a simple 1-level namespace where each table corresponds to a subdirectory with the format `<table_name>.lance`.
This mode is ideal for getting started quickly with Lance tables.

### Directory Layout

A directory catalog maps to a directory on storage, called the **catalog directory**.
A Lance table corresponds to a subdirectory in the catalog directory that has the format `<table_name>.lance`,
called a **table directory**.

Consider the following example catalog directory layout:

```
.
└── /my/dir1/
    ├── table1.lance/
    │   ├── data/
    │   │   ├── 0aa36d91-8293-406b-958c-faf9e7547938.lance
    │   │   └── ed7af55d-b064-4442-bcb5-47b524e98d0e.lance
    │   ├── _versions/
    │   │   └── 9223372036854775707.manifest
    │   └── _indices/
    │       └── 85814508-ed9a-41f2-b939-2050bb7a0ed5-fts/
    │           └── index.idx
    ├── table2.lance/
    ├── table3.lance/
    │   └── .lance-deregistered      # Marker: table3 is deregistered
    └── table4.lance/
        └── .lance-reserved          # Marker: table4 is reserved but not created
```

This describes a Lance Directory Catalog with the catalog directory at `/my/dir1/`.
It contains active tables `table1` and `table2` at table directories
`/my/dir1/table1.lance` and `/my/dir1/table2.lance`.
Table `table3` exists on storage but is deregistered (excluded from table listings).
Table `table4` is reserved but not yet created with data.

### Table Existence

In V1, a table exists in a Lance Directory Catalog if a table directory of the specific name exists
and the table is not marked as deregistered.
In object store terms, this means the prefix `<table_name>.lance/` has at least one file in it
and the file `<table_name>.lance/.lance-deregistered` does not exist.

### Marker Files

V1 uses marker files within table directories to track table state:

| Marker File           | Purpose                                                                 |
|-----------------------|-------------------------------------------------------------------------|
| `.lance-reserved`     | Indicates a table name/location is reserved but not yet created         |
| `.lance-deregistered` | Indicates a table has been deregistered but data is preserved           |

When a table is deregistered via the `DeregisterTable` operation, the `.lance-deregistered` marker file
is created inside the table directory. This causes the table to be excluded from `ListTables` results
and to return "not found" for `DescribeTable` and `TableExists` operations, while preserving the table data
for potential re-registration.

## V2: Manifest

V2 uses a special `__manifest` table (a Lance table) stored in the catalog directory to track all tables
and namespaces. This provides several advantages over V1:

- **Nested namespaces**: Support for hierarchical namespace organization
- **Better performance**: Table discovery queries the manifest table instead of scanning the directory and leverages Lance's random access capability.
- **Metadata support**: All operations can be supported, e.g. namespaces can have associated properties/metadata, tables can be renamed.
- **Optimized directory path**: Hash-based directory naming prevents conflicts and maximizes throughput in object storage.

Because the catalog metadata is itself stored as a Lance table, the catalog inherits the transactional semantics, snapshot isolation, and schema evolution guarantees of the table format, while also benefiting from Lance's random-access-friendly file layout and table-level indexing capabilities.

### Directory Layout

```
.
└── /my/dir1/
    ├── __manifest/                    # The manifest table
    │   ├── data/
    │   │   └── ...
    │   └── _versions/
    │       └── ...
    ├── table1.lance/                  # Root namespace table (compatibility mode)
    │   └── ...
    ├── a1b2c3d4_table2/               # Root namespace table (V2)
    │   └── ...
    └── e5f6g7h8_ns1$table3/           # Table in child namespace
        └── ...
```

### Manifest Table Schema

The `__manifest` table has the following schema:

| Column         | Type                    | Description                                                                                                                                                                     |
|----------------|-------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `object_id`    | String                  | Unique identifier for the object. For root-level objects, this is the name. For nested objects, this is the namespace path joined by `$` delimiter (e.g., `ns1$ns2$table_name`) |
| `object_type`  | String                  | Either `"namespace"` or `"table"`                                                                                                                                               |
| `location`     | String (nullable)       | Relative path to the table directory within the root (only for tables)                                                                                                          |
| `metadata`     | String (nullable)       | JSON-encoded metadata/properties (only for namespaces)                                                                                                                          |
| `base_objects` | List<String> (nullable) | Reserved for future use (e.g., view dependencies)                                                                                                                               |

**Primary Key**: The `object_id` column is the [unenforced primary key](https://lance.org/format/table/#unenforced-primary-key) for the manifest table. Implementation of this spec must always enforce the primary key uniqueness using features like Lance merge insert with primary key deduplication.

**Schema Extensibility**: The `__manifest` table schema may include additional columns beyond those listed above. Implementations should preserve unrecognized columns during updates, since extensions may add columns for filtering or other metadata-driven behaviors.

### Root Namespace Properties

In V2, the root namespace is implicit and does not have a row in the `__manifest` table. Instead, root namespace properties are stored in the `__manifest` Lance table's metadata map. Properties are stored as key-value pairs where the key is the property name and the value is a UTF-8 encoded byte array.

For example, implementations may store catalog-level properties in the `__manifest` table's metadata.

### Manifest Table Indexes

The following indexes are created on the manifest table for query performance:

- BTREE index on `object_id` for fast lookups
- Bitmap index on `object_type` for efficient type filtering
- LabelList index on `base_objects` for view dependency queries

### Manifest Table Commits

When adding a new entry in the manifest table, it must atomically check if the table already exists such entry,
as well as if any concurrent operation writes the same entry, and fail the operation accordingly if such conflict exists.

### Manifest Table Directory

In V2, table data is stored in directories with hash-based names in the format `<hash>_<object_id>`.
For example, a table `my_table` in namespace `ns1` would be stored in a directory like `a1b2c3d4_ns1$my_table`.

The hash prefix serves two purposes:

1. **Object store throughput**: Many object stores (e.g., S3) partition data by key prefix. Random hash prefixes distribute tables across partitions for better parallelism.
2. **Conflict prevention**: High entropy prevents issues when a table is created, deleted, and recreated with the same name in quick succession.

The `object_id` suffix ensures uniqueness and aids debugging.

In [compatibility mode](#compatibility-mode), root namespace tables use `<table_name>.lance` naming to remain compatible with V1.


### Table Version Management

V2 optionally supports managed table versioning, where table versions are tracked in the `__manifest` table instead of relying on Lance's native version management. When enabled, the directory catalog acts as an [external manifest store](https://lance.org/format/table/transaction/#external-manifest-store). This feature must be enabled for the entire catalog.

#### Enabling Table Version Management

To enable table version management, store `table_version_management=true` in the `__manifest` Lance table's metadata map. Once enabled, all table version operations must use the namespace APIs (`CreateTableVersion`, `BatchCreateTableVersions`, `DescribeTableVersion`, `ListTableVersions`, `BatchDeleteTableVersions`) instead of the default single-table storage-only version management.

#### Table Version Object ID

Table versions are stored in the `__manifest` table with `object_id` in the format `<table_id>$<version>`. For example:

- Table `users` version 1: `object_id = "users$1"`
- Table `analytics$events` (in namespace `analytics`) version 5: `object_id = "analytics$events$5"`

The `object_type` for table version entries is `"table_version"`.

#### Table Version Metadata Schema

The `metadata` column for table version entries contains a JSON object with the following schema:

| Field           | Type                        | Required | Description                                                                                                                                                                                                                     |
|-----------------|-----------------------------|----------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `manifest_path` | string                      | Yes      | Path to the manifest file for this version                                                                                                                                                                                      |
| `manifest_size` | integer                     | No       | Size of the manifest file in bytes                                                                                                                                                                                              |
| `e_tag`         | string                      | No       | ETag for the manifest file, useful for S3 and similar object stores                                                                                                                                                             |
| `metadata`      | object (string -> string)   | No       | Optional key-value pairs of version metadata                                                                                                                                                                                    |
| `naming_scheme` | string                      | No       | The [naming scheme](https://lance.org/format/table/transaction/#manifest-naming-schemes) used for manifest files                                                                                                                |

Example metadata JSON:

```json
{
    "manifest_path": "_versions/9223372036854775806.manifest",
    "manifest_size": 4096,
    "e_tag": "abc123",
    "metadata": {"author": "user1", "description": "Initial schema"},
    "naming_scheme": "V2"
}
```

## Compatibility Mode

By default, the directory catalog operates in compatibility mode, supporting both V1 and V2 tables simultaneously. This allows gradual migration from V1 to V2 without disrupting existing workflows.

In compatibility mode:

1. When checking if a table exists in the root namespace, the implementation first checks the manifest table, then falls back to checking if a `<table_name>.lance` directory exists.
2. When listing tables in the root namespace, results from both the manifest table and directory listing are merged, with manifest entries taking precedence when duplicates exist.
3. When creating tables in the root namespace, the table is registered in the manifest and uses the V1 `<table_name>.lance` naming convention for backward compatibility.
4. If a table in the root namespace is renamed, it transitions to the V2 hash-based path naming.
5. For operations in child namespaces, only V2 behavior is used since V1 does not support nested namespaces.

### Migration from V1 to V2

To fully migrate from V1 to V2, add all existing V1 table directory paths to the manifest table. Once all tables are registered in the manifest, compatibility mode can be disabled to use only V2 behavior.



================================================
FILE: docs/src/catalog/rest/index.md
================================================
# Lance REST Catalog

In enterprise environments, ML teams often must integrate with existing catalog systems to satisfy governance, access control, and compliance requirements. The **Lance REST Catalog** is an OpenAPI protocol that enables reading, writing, and managing Lance tables by connecting to metadata services or building a custom metadata server in a standardized way.

The REST Catalog specification, defined as an OpenAPI document, describes the data models and metadata operations needed to discover and manage Lance tables. It also defines data operations such as `QueryTable` and `InsertIntoTable` which exchange Arrow record batches via Apache Arrow IPC streams for efficient data transfer and interoperability with Arrow-native compute engines.

The REST server definition can be found in the [OpenAPI specification](https://editor-next.swagger.io/?url=https://raw.githubusercontent.com/lance-format/lance-namespace/refs/heads/main/docs/src/spec.yaml).

## External Manifest Store

The REST Catalog also exposes table version management APIs that can act as an external manifest store. When used, table commits are coordinated through the catalog before the resulting table metadata is written to storage. This enables organizations to enforce governance policies such as auditing, access control, and commit validation while still preserving the Lance table format as the authoritative source of table state.

## Duality with Namespace Client Spec

The Lance Namespace Client spec defines request and response models using OpenAPI.
The REST Catalog spec leverages this fact — the REST API is largely identical to the Namespace Client spec,
with the request and response schemas directly used as HTTP request and response bodies.

This duality minimizes data conversion between client and server:
a client can serialize its request model directly to JSON for the HTTP body,
and deserialize the HTTP response body directly into the response model.

There are a few exceptions where the REST spec diverges from the Namespace Client spec.
For example, for some operations like `InsertIntoTable`, `CreateTable`, `MergeInsertIntoTable`,
the HTTP request body is used for transmitting Arrow IPC binary data,
and the operation request fields are transmitted through query parameters instead.
For some list operations like `ListNamespaces` and `ListTables`,
pagination tokens and limits may be passed as query parameters
for easier URL construction and caching.

These non-standard operations are documented in the [Non-Standard Operations](#non-standard-operations) section below.

## REST Routes

The REST route for an operation typically follows the pattern of `POST /<version>/<object>/{id}/<action>`,
for example `POST /v1/namespace/{id}/list` for `ListNamespace`.
The request and response schemas are used as the actual request and response of the route.

The key design principle of the REST route is that all the necessary information for a reverse proxy
(e.g. load balancing, authN, authZ) should be available for access without the need to deserialize request body.
For example, the route for `CreateTable` is `POST /v1/table/{id}/create` instead of `POST /v1/table`
so that the table identifier is visible to the reverse proxy without parsing the request body.

## Standard Operations

Standard operations should take the same request and return the same response as any other implementation.

The information in the route could also present in the request body.
When the information in the route and request body both present but do not match, the server must throw a 400 Bad Request error.
When the information in the request body is missing, the server must use the information in the route instead.

## Identity Header Mapping

All request schemas include an optional `identity` field for authentication.
For REST Catalog, the identity fields are mapped to HTTP headers:

| Identity Field | REST Form       | Location |
|----------------|-----------------|----------|
| `api_key`      | `x-api-key`     | Header   |
| `auth_token`   | `Authorization` | Header   |

The `auth_token` is sent using the Bearer scheme (e.g., `Authorization: Bearer <token>`).

When identity information is provided in both the request body and headers, the header values take precedence.

## Context Header Mapping

All request and response schemas include an optional `context` field for passing arbitrary
key-value pairs. This allows clients to send implementation-specific context to the server,
and the server to return implementation-specific context back to the client.

For REST Catalog, context entries are mapped to and from HTTP headers using the `header.`
prefix:

| Direction | Context Entry                  | REST Form                          |
|-----------|--------------------------------|------------------------------------|
| Request   | `{"header.<name>": "<value>"}` | request header `<name>: <value>`   |
| Response  | `{"header.<name>": "<value>"}` | response header `<name>: <value>`  |

On a request, any context entry whose key starts with `header.` is sent as an HTTP request
header with the prefix stripped. For example, a context entry
`{"header.x-trace-id": "abc123", "header.x-user-region": "us-west"}` would be sent as:

```
x-trace-id: abc123
x-user-region: us-west
```

On a response, every HTTP response header is returned as a context entry whose key is the
header name prefixed with `header.`. For example, the response headers:

```
x-trace-id: abc123
x-user-region: us-west
```

would be returned as the context entry
`{"header.x-trace-id": "abc123", "header.x-user-region": "us-west"}`.

How to use the context is custom to the specific implementation.
Common use cases include:

- Passing trace IDs for distributed tracing
- Forwarding user context to downstream services
- Providing hints to the implementation for optimization

## Non-Standard Operations

For request and response that cannot be simply described as a JSON object
the REST server needs to perform special handling to describe equivalent information through path parameters,
query parameters and headers.

### ListNamespaces

**Route:** `GET /v1/namespace/{id}/list`

Uses GET without a request body. Pagination parameters are passed as query parameters.

| Request Field | REST Form    | Location        |
|---------------|--------------|-----------------|
| `id`          | `{id}`       | Path parameter  |
| `page_token`  | `page_token` | Query parameter |
| `limit`       | `limit`      | Query parameter |

### ListTables

**Route:** `GET /v1/namespace/{id}/table/list`

Uses GET without a request body. Pagination parameters are passed as query parameters.

| Request Field | REST Form    | Location        |
|---------------|--------------|-----------------|
| `id`          | `{id}`       | Path parameter  |
| `page_token`  | `page_token` | Query parameter |
| `limit`       | `limit`      | Query parameter |

### ListAllTables

**Route:** `GET /v1/table/`

Uses GET without a request body. Pagination parameters are passed as query parameters.

| Request Field | REST Form | Location |
|---------------|-----------|----------|
| `page_token` | `page_token` | Query parameter |
| `limit` | `limit` | Query parameter |
| `delimiter` | `delimiter` | Query parameter |

### DescribeTable

**Route:** `POST /v1/table/{id}/describe`

The `with_table_uri`, `load_detailed_metadata`, and `check_declared` fields are passed as query parameters instead of in the request body.

| Request Field            | REST Form                | Location        |
|--------------------------|--------------------------|-----------------|
| `id`                     | `{id}`                   | Path parameter  |
| `with_table_uri`         | `with_table_uri`         | Query parameter |
| `load_detailed_metadata` | `load_detailed_metadata` | Query parameter |
| `check_declared`         | `check_declared`         | Query parameter |

### CreateTable

**Route:** `POST /v1/table/{id}/create`

**Content-Type:** `application/vnd.apache.arrow.stream`

The request body contains Arrow IPC stream data. The table schema is derived from the Arrow stream schema.
If the stream is empty, an empty table is created.

| Request Field | REST Form                  | Location                         |
|---------------|----------------------------|----------------------------------|
| `id`          | `{id}`                     | Path parameter                   |
| `mode`        | `mode`                     | Query parameter                  |
| `location`    | `x-lance-table-location`   | Header                           |
| `properties`  | `x-lance-table-properties` | Header (JSON-encoded string map) |
| `data`        | Request body               | Body (Arrow IPC stream)          |

### InsertIntoTable

**Route:** `POST /v1/table/{id}/insert`

**Content-Type:** `application/vnd.apache.arrow.stream`

The request body contains Arrow IPC stream data with records to insert.

| Request Field | REST Form    | Location                                                     |
|---------------|--------------|--------------------------------------------------------------|
| `id`          | `{id}`       | Path parameter                                               |
| `mode`        | `mode`       | Query parameter (`append` or `overwrite`, default: `append`) |
| `data`        | Request body | Body (Arrow IPC stream)                                      |

### MergeInsertIntoTable

**Route:** `POST /v1/table/{id}/merge_insert`

**Content-Type:** `application/vnd.apache.arrow.stream`

The request body contains Arrow IPC stream data. Performs a merge insert (upsert) operation
that updates existing rows based on a matching column and inserts new rows that don't match.

| Request Field                            | REST Form                                | Location                                             |
|------------------------------------------|------------------------------------------|------------------------------------------------------|
| `id`                                     | `{id}`                                   | Path parameter                                       |
| `on`                                     | `on`                                     | Query parameter (required)                           |
| `when_matched_update_all`                | `when_matched_update_all`                | Query parameter (boolean)                            |
| `when_matched_update_all_filt`           | `when_matched_update_all_filt`           | Query parameter (SQL expression)                     |
| `when_not_matched_insert_all`            | `when_not_matched_insert_all`            | Query parameter (boolean)                            |
| `when_not_matched_by_source_delete`      | `when_not_matched_by_source_delete`      | Query parameter (boolean)                            |
| `when_not_matched_by_source_delete_filt` | `when_not_matched_by_source_delete_filt` | Query parameter (SQL expression)                     |
| `timeout`                                | `timeout`                                | Query parameter (duration string, e.g., "30s", "5m") |
| `use_index`                              | `use_index`                              | Query parameter (boolean)                            |
| `data`                                   | Request body                             | Body (Arrow IPC stream)                              |

### QueryTable

**Route:** `POST /v1/table/{id}/query`

**Response Content-Type:** `application/vnd.apache.arrow.file`

The response body contains Arrow IPC file data instead of JSON. It maps to the
`QueryTableResponse` model as follows:

| Response Field | REST Form                     | Notes                                                   |
|----------------|-------------------------------|---------------------------------------------------------|
| `data`         | Response body                 | Arrow IPC file (binary, not JSON)                       |
| `context`      | Response headers (`header.*`) | Each response header maps to a `header.`-prefixed entry |

### CountTableRows

**Route:** `POST /v1/table/{id}/count_rows`

The response is returned as a plain integer instead of a JSON object. It maps to the
`CountTableRowsResponse` model as follows:

| Response Field | REST Form                     | Notes                                                   |
|----------------|-------------------------------|---------------------------------------------------------|
| `count`        | Response body                 | Plain integer (not JSON wrapped)                        |
| `context`      | Response headers (`header.*`) | Each response header maps to a `header.`-prefixed entry |

### NamespaceExists

**Route:** `POST /v1/namespace/{id}/exists`

Existence is conveyed through the HTTP status code with no response body. The response maps
to the `NamespaceExistsResponse` model as follows:

| Response Field | REST Form                     | Notes                                                   |
|----------------|-------------------------------|---------------------------------------------------------|
| (existence)    | HTTP status code              | `200` if the namespace exists, `404` otherwise          |
| `context`      | Response headers (`header.*`) | Each response header maps to a `header.`-prefixed entry |

### TableExists

**Route:** `POST /v1/table/{id}/exists`

Existence is conveyed through the HTTP status code with no response body. The response maps
to the `TableExistsResponse` model as follows:

| Response Field | REST Form                     | Notes                                                   |
|----------------|-------------------------------|---------------------------------------------------------|
| (existence)    | HTTP status code              | `200` if the table exists, `404` otherwise              |
| `context`      | Response headers (`header.*`) | Each response header maps to a `header.`-prefixed entry |

### DropTable

**Route:** `POST /v1/table/{id}/drop`

No request body. All parameters are in the path.

### DropTableIndex

**Route:** `POST /v1/table/{id}/index/{index_name}/drop`

No request body. All parameters are in the path.

### ListTableVersions

**Route:** `POST /v1/table/{id}/version/list`

No request body. Pagination parameters are passed as query parameters.

| Request Field | REST Form    | Location        |
|---------------|--------------|-----------------|
| `id`          | `{id}`       | Path parameter  |
| `page_token`  | `page_token` | Query parameter |
| `limit`       | `limit`      | Query parameter |

### ListTableTags

**Route:** `POST /v1/table/{id}/tags/list`

No request body. Pagination parameters are passed as query parameters.

| Request Field | REST Form    | Location        |
|---------------|--------------|-----------------|
| `id`          | `{id}`       | Path parameter  |
| `page_token`  | `page_token` | Query parameter |
| `limit`       | `limit`      | Query parameter |

### ExplainTableQueryPlan

**Route:** `POST /v1/table/{id}/explain_plan`

The response is returned as a plain string instead of a JSON object.

| Request Field | REST Form | Location           |
|---------------|-----------|--------------------|
| `id`          | `{id}`    | Path parameter     |
| `query`       | `query`   | Request body field |
| `verbose`     | `verbose` | Request body field |

| Response Field | REST Form     | Notes                           |
|----------------|---------------|---------------------------------|
| `plan`         | Response body | Plain string (not JSON wrapped) |

### AnalyzeTableQueryPlan

**Route:** `POST /v1/table/{id}/analyze_plan`

The response is returned as a plain string instead of a JSON object.

| Request Field | REST Form | Location           |
|---------------|-----------|--------------------|
| `id`          | `{id}`    | Path parameter     |
| `query`       | `query`   | Request body field |

| Response Field | REST Form     | Notes                           |
|----------------|---------------|---------------------------------|
| `analysis`     | Response body | Plain string (not JSON wrapped) |

### UpdateTableSchemaMetadata

**Route:** `POST /v1/table/{id}/schema_metadata/update`

Both request and response bodies are direct objects (map of string to string) instead of being wrapped in a `metadata` field.

| Request Field | REST Form    | Location                                                          |
|---------------|--------------|-------------------------------------------------------------------|
| `id`          | `{id}`       | Path parameter                                                    |
| `metadata`    | Request body | Direct object `{"key": "value", ...}` (not `{"metadata": {...}}`) |

| Response Field | REST Form     | Notes                                                             |
|----------------|---------------|-------------------------------------------------------------------|
| `metadata`     | Response body | Direct object `{"key": "value", ...}` (not `{"metadata": {...}}`) |

## REST Catalog Server and Adapter

Any REST HTTP server that implements this OpenAPI protocol is called a **Lance REST Catalog server**.
If you are a metadata service provider that is building a custom implementation of Lance catalog,
building a REST server gives you standardized integration to Lance
without the need to worry about tool support and
continuously distribute newer library versions compared to using an implementation.

If the main purpose of this server is to be a proxy on top of an existing metadata service,
converting back and forth between Lance REST API models and native API models of the metadata service,
then this Lance REST Catalog server is called a **Lance Catalog adapter**.

## Choosing between an Adapter vs an Implementation

Any adapter can always be directly a Lance catalog implementation bypassing the REST server,
and vise versa. In fact, an implementation is basically the backend of an adapter.
For example, we natively support a Lance HMS Catalog implementation,
as well as a Lance catalog adapter for HMS by using the HMS Catalog implementation to fulfill requests in the Lance REST server.

If you are considering between a Lance catalog adapter vs implementation to build or use in your environment,
here are some criteria to consider:

1. **Multi-Language Feasibility & Maintenance Cost**: If you want a single strategy that works across all Lance language bindings, an adapter is preferred.
   Sometimes it is not even possible for an integration to go with the implementation approach since it cannot support all the languages.
   Sometimes an integration is popular or important enough that it is viable to build an implementation and maintain one library per language.
2. **Tooling Support**: each tool needs to declare the Lance catalog implementations it supports.
   That means there will be a preference for tools to always support a REST catalog,
   but it might not always support a specific implementation. This favors the adapter approach.
3. **Security**: if you have security concerns about the adapter being a man-in-the-middle, you should choose an implementation
4. **Performance**: after all, adapter adds one layer of indirection and is thus not the most performant solution.
   If you are performance sensitive, you should choose an implementation



================================================
FILE: docs/src/namespace/index.md
================================================
# Lance Namespace Client Spec

The **Lance Namespace Client Spec** defines a standardized interface for catalog interactions such as table discovery, resolving table locations, and coordinating commits. It abstracts both the [Directory Catalog](../catalog/dir/index.md) (operating as an in-process library) and the [REST Catalog](../catalog/rest/index.md) (designed for client-server deployments) behind a single interface called `LanceNamespace`.

![Namespace Overview](../overview.png)

## Why "Namespace" Instead of "Catalog"?

We use the term **Namespace** rather than **Catalog** because we want a generic term that fits into any hierarchical structure. Different systems use different names for their organizational units:

| System                | Container Concepts                          |
|-----------------------|---------------------------------------------|
| Apache Hive           | Metastore → Database → Table                |
| Unity Catalog         | Metastore → Catalog → Schema → Table        |
| Apache Polaris        | Catalog → Namespace (arbitrary levels) → Table     |
| Directory Storage     | Root directory → Tables                     |

The Lance Namespace Client provides a **unified framework** across all of these systems. A "namespace" in Lance can represent a catalog, schema, metastore, database, metalake, or any other hierarchical container — the spec abstracts away these differences.

This further enables integration with external catalog specifications such as the Apache Iceberg REST Catalog, Apache Hive Metastore, Unity Catalog, and Apache Polaris Catalog.

## Examples

The following examples show how different catalog systems map to Lance Namespace.

### Directory (1-level)

The simplest case: tables directly in a storage directory, a common use case for ML/AI scientists:

| Directory       | Lance Namespace    |
|-----------------|--------------------|
| /data/          | Root Namespace     |
| └─ users.lance  | Table `["users"]`  |
| └─ orders.lance | Table `["orders"]` |

### Unity Catalog (3-level)

Unity Catalog uses a 3-level hierarchy under a metastore (one metastore per server):

| Unity Catalog                            | Lance Namespace                        |
|------------------------------------------|----------------------------------------|
| Root Metastore                           | Root Namespace                         |
| └─ Catalog "prod"                        | Namespace `["prod"]`                   |
| &emsp;&emsp;└─ Schema "analytics"        | Namespace `["prod", "analytics"]`      |
| &emsp;&emsp;&emsp;&emsp;└─ Table "users" | Table `["prod", "analytics", "users"]` |

### Apache Polaris (flexible levels)

Apache Polaris supports arbitrary namespace nesting:

| Polaris                                              | Lance Namespace                           |
|------------------------------------------------------|-------------------------------------------|
| Root Catalog                                         | Root Namespace                            |
| └─ Namespace "prod"                                  | Namespace `["prod"]`                      |
| &emsp;&emsp;└─ Namespace "team_a"                    | Namespace `["prod", "team_a"]`            |
| &emsp;&emsp;&emsp;&emsp;└─ Namespace "ml"            | Namespace `["prod", "team_a", "ml"]`      |
| &emsp;&emsp;&emsp;&emsp;&emsp;&emsp;└─ Table "model" | Table `["prod", "team_a", "ml", "model"]` |

## Engine Interoperability

Because compute engines interact with catalogs through the Lance Namespace interface, they can work with Lance tables regardless of how the catalog is implemented or structured. Systems such as Apache DataFusion, Apache Spark, and Ray can interact with Lance tables through this interface, enabling distributed query execution, table maintenance, and multi-table workflows while remaining agnostic to the underlying catalog deployment.

For each programming language, a Lance Namespace Client provides a unified interface that compute engines can integrate against. For example:

- **Java SDK** (`org.lance:lance-namespace-core`): Enables engines like Apache Spark, Apache Flink, Apache Kafka, Trino, Presto, etc. to build their Lance connectors.
- **Python SDK** (`lance_namespace`): Enables frameworks like Ray, Dask, and MLflow to work with Lance tables.
- **Rust SDK** (`lance-namespace`): The core interface used by native implementations.

Each catalog spec has corresponding implementations in supported languages that fulfill the Namespace Client interface/trait.

![Namespace Java SDK Example](../java-sdk-example.png)



================================================
FILE: docs/src/namespace/object-relationship.md
================================================
# Objects & Relationships

This page describes the objects in a namespace and their relationships with each other.

## Namespace Definition

A namespace is a centralized repository for discovering, organizing, and managing tables.
It can not only contain a collection of tables, but also a collection of namespaces recursively.
It is designed to encapsulates concepts including namespace, metastore, database, schema, etc.
that frequently appear in other similar data systems to allow easy integration with any system of any type of object hierarchy.

Here is an example layout of a namespace:

```text
Root namespace
├── Namespace "cat2"
│   └── Namespace "cat5"
│       └── Table "t1"
├── Namespace "cat3"
└── Namespace "cat4"
    ├── Table "t3"
    └── Table "t4"
```

## Parent & Child

We use the term **parent** and **child** to describe relationship between 2 objects.
If namespace A directly contains B, then A is the parent namespace of B, i.e. B is a child of A.
For examples:

- Namespace `ns1` contains a **child namespace** `ns4`. i.e. `ns1` is the **parent namespace** of `ns4`.
- Namespace `ns2` contains a **child table** `t2`, i.e. `t2` belongs to **parent namespace** `ns2`.

## Root Namespace

A root namespace is a namespace that has no parent.
The root namespace is assumed to always exist and is ready to be connected to by a tool to explore objects in the namespace.
The lifecycle management (e.g. creation, deletion) of the root namespace is out of scope of this specification.

## Object Name

The **name** of an object is a string that uniquely identifies the object within the parent namespace it belongs to.
The name of any object must be unique among all other objects that share the same parent namespace.
For examples:

- `cat2`, `cat3` and `cat4` are all unique names under the root namespace
- `t3` and `t4` are both unique names under `cat4`

## Object Identifier

The **identifier** of an object uniquely identifies the object within the root namespace it belongs to.
The identifier of any object must be unique among all other objects that share the same root namespace.

Based on the uniqueness property of an object name within its parent namespace,
an object identifier is the list of object names starting from (not including) the root namespace to (including) the object itself.
This is also called an **list style identifier**.
For examples:

- the list style identifier of `cat5` is `[cat2, cat5]`
- the list style identifier of `t1` is `[cat2, cat5, t1]`

The dollar (`$`) symbol is used as the default delimiter to join all the names to form an **string style identifier**,
but other symbols could also be used if the dollar sign is used in the object name.
For examples:

- the string style identifier of `cat5` is `cat2$cat5`
- the string style identifier of `t1` is `cat2$cat5$t1`
- the string style identifier of `t3` is `cat4#t3` when using delimiter `#`

## Name and Identifier for Root Namespace

The root namespace itself has no name or identifier.
When represented in code, its name and string style identifier is represented by an empty or null string,
and its list style identifier is represented by an empty or null list.

The actual name and identifier of the root namespace is typically
assigned by users through some configuration when used in a tool.
For example, a root namespace can be called `cat1` in Ray, but called `cat2` in Apache Spark,
and they are both configured to connect to the same root namespace.

## Object Level

The root namespace is always at level 0.
This means if an object has list style identifier with list size `N`,
the object is at the `N`th level in the entire namespace hierarchy,
and its corresponding object identifier has `N` levels.
For examples, a namespace `[ns1, ns2]` is at level 2, and its identifier `ns1$ns2` has 2 levels.
A table `[catalog1, database2, table3]` is at level 3, and its identifier `catalog1$database2$table3` has 3 levels.

### Leveled Namespace

If every table in the root namespace are at the same level `N`, the namespace is called **leveled**,
and we say this namespace is a `N`-level namespace.
For example, a [Directory Catalog](../catalog/dir/index.md) is a 1-level namespace,
and a Hive 2.x namespace is a 2-level namespace.



================================================
FILE: docs/src/namespace/.pages
================================================
title: Namespace Client Spec
nav:
  - Overview: index.md
  - Objects & Relationships: object-relationship.md
  - operations
  - Supported Catalogs: supported-catalogs



================================================
FILE: docs/src/namespace/operations/errors.md
================================================
# Error Handling

All Lance Namespace operations use a standardized error model for
consistent error handling across different implementations and languages.

## Error Codes

Error codes are globally unique integers that identify the specific error type.
These codes are consistent across all Lance Namespace implementations (Python, Java, Rust, REST).

| Code | Name                       | Description                                       |
|------|----------------------------|---------------------------------------------------|
| 0    | Unsupported                | Operation not supported by this backend           |
| 1    | NamespaceNotFound          | The specified namespace does not exist            |
| 2    | NamespaceAlreadyExists     | A namespace with this name already exists         |
| 3    | NamespaceNotEmpty          | Namespace contains tables or child namespaces     |
| 4    | TableNotFound              | The specified table does not exist                |
| 5    | TableAlreadyExists         | A table with this name already exists             |
| 6    | TableIndexNotFound         | The specified table index does not exist          |
| 7    | TableIndexAlreadyExists    | A table index with this name already exists       |
| 8    | TableTagNotFound           | The specified table tag does not exist            |
| 9    | TableTagAlreadyExists      | A table tag with this name already exists         |
| 10   | TransactionNotFound        | The specified transaction does not exist          |
| 11   | TableVersionNotFound       | The specified table version does not exist        |
| 12   | TableColumnNotFound        | The specified table column does not exist         |
| 13   | InvalidInput               | Malformed request or invalid parameters           |
| 14   | ConcurrentModification     | Optimistic concurrency conflict                   |
| 15   | PermissionDenied           | User lacks permission for this operation          |
| 16   | Unauthenticated            | Authentication credentials are missing or invalid |
| 17   | ServiceUnavailable         | Service is temporarily unavailable                |
| 18   | Internal                   | Unexpected server/implementation error            |
| 19   | InvalidTableState          | Table is in an invalid state for the operation    |
| 20   | TableSchemaValidationError | Table schema validation failed                    |
| 21   | Throttling                 | Request rate limit exceeded                       |

## Per-Operation Errors

Each operation can return a specific set of errors.
The following sections document which errors are expected for each operation category.

### Common Errors

All operations may return the following errors:

- **0 (Unsupported)**: The operation is not supported by this backend
- **13 (InvalidInput)**: The request contains invalid parameters
- **15 (PermissionDenied)**: The user lacks permission for this operation
- **16 (Unauthenticated)**: Authentication credentials are missing or invalid
- **17 (ServiceUnavailable)**: The service is temporarily unavailable
- **18 (Internal)**: An unexpected internal error occurred
- **21 (Throttling)**: Request rate limit exceeded

### Namespace Metadata Operations

| Operation         | Additional Errors                            |
|-------------------|----------------------------------------------|
| CreateNamespace   | 2 (NamespaceAlreadyExists)                   |
| ListNamespaces    | 1 (NamespaceNotFound)                        |
| DescribeNamespace | 1 (NamespaceNotFound)                        |
| DropNamespace     | 1 (NamespaceNotFound), 3 (NamespaceNotEmpty) |
| NamespaceExists   | 1 (NamespaceNotFound)                        |
| ListTables        | 1 (NamespaceNotFound)                        |

### Table Metadata Operations

| Operation                 | Additional Errors                                                                                                                |
|---------------------------|----------------------------------------------------------------------------------------------------------------------------------|
| ListAllTables             | -                                                                                                                                |
| RegisterTable             | 1 (NamespaceNotFound), 5 (TableAlreadyExists), 14 (ConcurrentModification)                                                       |
| DescribeTable             | 1 (NamespaceNotFound), 4 (TableNotFound), 11 (TableVersionNotFound)                                                              |
| TableExists               | 1 (NamespaceNotFound), 4 (TableNotFound)                                                                                         |
| DropTable                 | 1 (NamespaceNotFound), 4 (TableNotFound)                                                                                         |
| DeregisterTable           | 1 (NamespaceNotFound), 4 (TableNotFound)                                                                                         |
| RestoreTable              | 1 (NamespaceNotFound), 4 (TableNotFound), 11 (TableVersionNotFound), 14 (ConcurrentModification)                                 |
| RenameTable               | 1 (NamespaceNotFound), 4 (TableNotFound), 5 (TableAlreadyExists), 14 (ConcurrentModification)                                    |
| GetTableStats             | 1 (NamespaceNotFound), 4 (TableNotFound)                                                                                         |
| AlterTableAlterColumns    | 1 (NamespaceNotFound), 4 (TableNotFound), 12 (TableColumnNotFound), 14 (ConcurrentModification), 20 (TableSchemaValidationError) |
| AlterTableDropColumns     | 1 (NamespaceNotFound), 4 (TableNotFound), 12 (TableColumnNotFound), 14 (ConcurrentModification)                                  |
| UpdateTableSchemaMetadata | 1 (NamespaceNotFound), 4 (TableNotFound), 14 (ConcurrentModification)                                                            |

### Table Data Operations

| Operation             | Additional Errors                                                                                                              |
|-----------------------|--------------------------------------------------------------------------------------------------------------------------------|
| InsertIntoTable       | 1 (NamespaceNotFound), 4 (TableNotFound), 14 (ConcurrentModification), 19 (InvalidTableState), 20 (TableSchemaValidationError) |
| MergeInsertIntoTable  | 1 (NamespaceNotFound), 4 (TableNotFound), 12 (TableColumnNotFound), 14 (ConcurrentModification), 19 (InvalidTableState)        |
| UpdateTable           | 1 (NamespaceNotFound), 4 (TableNotFound), 12 (TableColumnNotFound), 14 (ConcurrentModification), 19 (InvalidTableState)        |
| DeleteFromTable       | 1 (NamespaceNotFound), 4 (TableNotFound), 14 (ConcurrentModification), 19 (InvalidTableState)                                  |
| QueryTable            | 1 (NamespaceNotFound), 4 (TableNotFound), 11 (TableVersionNotFound), 12 (TableColumnNotFound)                                  |
| CountTableRows        | 1 (NamespaceNotFound), 4 (TableNotFound), 11 (TableVersionNotFound)                                                            |
| CreateTable           | 1 (NamespaceNotFound), 5 (TableAlreadyExists), 14 (ConcurrentModification), 20 (TableSchemaValidationError)                    |
| ExplainTableQueryPlan | 1 (NamespaceNotFound), 4 (TableNotFound)                                                                                       |
| AnalyzeTableQueryPlan | 1 (NamespaceNotFound), 4 (TableNotFound)                                                                                       |
| AlterTableAddColumns  | 1 (NamespaceNotFound), 4 (TableNotFound), 14 (ConcurrentModification), 20 (TableSchemaValidationError)                         |

### Index Metadata Operations

| Operation               | Additional Errors                                                                                                            |
|-------------------------|------------------------------------------------------------------------------------------------------------------------------|
| CreateTableIndex        | 1 (NamespaceNotFound), 4 (TableNotFound), 7 (TableIndexAlreadyExists), 12 (TableColumnNotFound), 14 (ConcurrentModification) |
| CreateTableScalarIndex  | 1 (NamespaceNotFound), 4 (TableNotFound), 7 (TableIndexAlreadyExists), 12 (TableColumnNotFound), 14 (ConcurrentModification) |
| ListTableIndices        | 1 (NamespaceNotFound), 4 (TableNotFound)                                                                                     |
| DescribeTableIndexStats | 1 (NamespaceNotFound), 4 (TableNotFound), 6 (TableIndexNotFound)                                                             |
| DropTableIndex          | 1 (NamespaceNotFound), 4 (TableNotFound), 6 (TableIndexNotFound)                                                             |

### Tag Metadata Operations

| Operation          | Additional Errors                                                                                                           |
|--------------------|-----------------------------------------------------------------------------------------------------------------------------|
| ListTableTags      | 1 (NamespaceNotFound), 4 (TableNotFound)                                                                                    |
| GetTableTagVersion | 1 (NamespaceNotFound), 4 (TableNotFound), 8 (TableTagNotFound)                                                              |
| CreateTableTag     | 1 (NamespaceNotFound), 4 (TableNotFound), 9 (TableTagAlreadyExists), 11 (TableVersionNotFound), 14 (ConcurrentModification) |
| DeleteTableTag     | 1 (NamespaceNotFound), 4 (TableNotFound), 8 (TableTagNotFound)                                                              |
| UpdateTableTag     | 1 (NamespaceNotFound), 4 (TableNotFound), 8 (TableTagNotFound), 11 (TableVersionNotFound), 14 (ConcurrentModification)      |

### Table Version Metadata Operations

| Operation                 | Additional Errors                                                     |
|---------------------------|-----------------------------------------------------------------------|
| ListTableVersions         | 1 (NamespaceNotFound), 4 (TableNotFound)                              |
| DescribeTableVersion      | 1 (NamespaceNotFound), 4 (TableNotFound), 11 (TableVersionNotFound)   |
| CreateTableVersion        | 1 (NamespaceNotFound), 4 (TableNotFound), 14 (ConcurrentModification) |
| BatchCreateTableVersions  | 1 (NamespaceNotFound), 4 (TableNotFound), 14 (ConcurrentModification) |
| BatchDeleteTableVersions  | 1 (NamespaceNotFound), 4 (TableNotFound)                              |

### Transaction Metadata Operations

| Operation           | Additional Errors                                     |
|---------------------|-------------------------------------------------------|
| DescribeTransaction | 10 (TransactionNotFound)                              |
| AlterTransaction    | 10 (TransactionNotFound), 14 (ConcurrentModification) |



================================================
FILE: docs/src/namespace/operations/index.md
================================================
# Namespace Operations

The Lance Namespace Specification defines a list of operations that can be performed against any Lance namespace.

## OpenAPI Standardization

The spec uses [OpenAPI](https://www.openapis.org/) to define the request and response models for each operation.
This standardization allows clients in any language to generate a client library from the
[OpenAPI specification](https://editor-next.swagger.io/?url=https://raw.githubusercontent.com/lance-format/lance-namespace/refs/heads/main/docs/src/spec.yaml)
and use it to invoke operations with the corresponding request model, receiving responses in the expected response model.

The actual execution of an operation can be:

- **Client-side**: The operation is executed entirely within the client (e.g., directory namespace)
- **Server-side**: The operation is sent to a remote server for execution (e.g., REST namespace)
- **Hybrid**: A combination of both, depending on the integrated catalog spec and service

This flexibility allows the same client interface to work across different namespace implementations
while maintaining consistent request/response contracts.

## Duality with REST Catalog Spec

The request and response models defined here are designed to work seamlessly with the
[REST Catalog](../../catalog/rest/index.md) spec. The REST Catalog uses these same schemas directly as
HTTP request and response bodies, minimizing data conversion between client and server.

This duality explains why certain fields like `id` are marked as optional in the request models:

- **In REST Catalog Spec**: The object identifier is already present in the REST route path
  (e.g., `/v1/table/{id}/describe`), so the `id` field in the request body is optional and
  can be omitted to avoid redundancy.
- **In Namespace Client Spec**: When invoking operations directly through a client library
  (e.g., for directory catalog), the `id` field **must be specified** in the request since
  there is no REST route to carry this information.

When both the route path and request body contain the `id`, the REST server must validate
that they match and return a 400 Bad Request error if they differ.
See [REST Routes](../../catalog/rest/index.md#rest-routes) for more details.

## Operation List

| Operation ID              | Current Version | Namespace | Table | Index | Metadata | Data | Transaction |
|---------------------------|-----------------|-----------|-------|-------|----------|------|-------------|
| CreateNamespace           | 1               | ✓         |       |       | ✓        |      |             |
| ListNamespaces            | 1               | ✓         |       |       | ✓        |      |             |
| DescribeNamespace         | 1               | ✓         |       |       | ✓        |      |             |
| DropNamespace             | 1               | ✓         |       |       | ✓        |      |             |
| NamespaceExists           | 1               | ✓         |       |       | ✓        |      |             |
| ListTables                | 1               | ✓         | ✓     |       | ✓        |      |             |
| ListAllTables             | 1               |           | ✓     |       | ✓        |      |             |
| RegisterTable             | 1               |           | ✓     |       | ✓        |      |             |
| DescribeTable             | 1               |           | ✓     |       | ✓        |      |             |
| TableExists               | 1               |           | ✓     |       | ✓        |      |             |
| DropTable                 | 1               |           | ✓     |       | ✓        |      |             |
| DeregisterTable           | 1               |           | ✓     |       | ✓        |      |             |
| InsertIntoTable           | 1               |           | ✓     |       |          | ✓    |             |
| MergeInsertIntoTable      | 1               |           | ✓     |       |          | ✓    |             |
| UpdateTable               | 1               |           | ✓     |       |          | ✓    |             |
| DeleteFromTable           | 1               |           | ✓     |       |          | ✓    |             |
| QueryTable                | 1               |           | ✓     |       |          | ✓    |             |
| CountTableRows            | 1               |           | ✓     |       |          | ✓    |             |
| CreateTable               | 1               |           | ✓     |       |          | ✓    |             |
| DeclareTable              | 1               |           | ✓     |       | ✓        |      |             |
| CreateTableIndex          | 1               |           | ✓     | ✓     | ✓        |      |             |
| CreateTableScalarIndex    | 1               |           | ✓     | ✓     | ✓        |      |             |
| ListTableIndices          | 1               |           | ✓     | ✓     | ✓        |      |             |
| DescribeTableIndexStats   | 1               |           | ✓     | ✓     | ✓        |      |             |
| RestoreTable              | 1               |           | ✓     |       | ✓        |      |             |
| RenameTable               | 1               |           | ✓     |       | ✓        |      |             |
| ListTableVersions         | 1               |           | ✓     |       | ✓        |      |             |
| CreateTableVersion        | 1               |           | ✓     |       | ✓        |      |             |
| BatchCreateTableVersions  | 1               |           | ✓     |       | ✓        |      |             |
| DescribeTableVersion      | 1               |           | ✓     |       | ✓        |      |             |
| BatchDeleteTableVersions  | 1               |           | ✓     |       | ✓        |      |             |
| ExplainTableQueryPlan     | 1               |           | ✓     |       |          | ✓    |             |
| AnalyzeTableQueryPlan     | 1               |           | ✓     |       |          | ✓    |             |
| AlterTableAddColumns      | 1               |           | ✓     |       |          | ✓    |             |
| AlterTableAlterColumns    | 1               |           | ✓     |       | ✓        |      |             |
| AlterTableBackfillColumns | 1               |           | ✓     |       |          | ✓    |             |
| AlterTableDropColumns     | 1               |           | ✓     |       | ✓        |      |             |
| RefreshMaterializedView   | 1               |           | ✓     |       |          | ✓    |             |
| UpdateFieldMetadata       | 1               |           | ✓     |       | ✓        |      |             |
| UpdateTableSchemaMetadata | 1               |           | ✓     |       | ✓        |      |             |
| GetTableStats             | 1               |           | ✓     |       | ✓        |      |             |
| ListTableTags             | 1               |           | ✓     |       | ✓        |      |             |
| GetTableTagVersion        | 1               |           | ✓     |       | ✓        |      |             |
| CreateTableTag            | 1               |           | ✓     |       | ✓        |      |             |
| DeleteTableTag            | 1               |           | ✓     |       | ✓        |      |             |
| UpdateTableTag            | 1               |           | ✓     |       | ✓        |      |             |
| ListTableBranches         | 1               |           | ✓     |       | ✓        |      |             |
| CreateTableBranch         | 1               |           | ✓     |       | ✓        |      |             |
| DeleteTableBranch         | 1               |           | ✓     |       | ✓        |      |             |
| DropTableIndex            | 1               |           | ✓     | ✓     | ✓        |      |             |
| DescribeTransaction       | 1               |           |       |       | ✓        |      | ✓           |
| AlterTransaction          | 1               |           |       |       | ✓        |      | ✓           |

## Recommended Basic Operations

To have a functional basic namespace implementation,
the following metadata operations are recommended as a minimum:

**Namespace Metadata Operations:**

- CreateNamespace - Create a new namespace
- ListNamespaces - List available namespaces
- DescribeNamespace - Get namespace details
- DropNamespace - Remove a namespace

**Table Metadata Operations:**

- DeclareTable - Declare a table as exist
- ListTables - List tables in a namespace
- DescribeTable - Get table details
- DeregisterTable - Unregister a table while preserving its data

These operations provide the foundational metadata management capabilities needed for namespace and table administration
without requiring data or index operation support. With the namespace able to provide basic information about the table,
the Lance SDK can be used to fulfill the other operations.

### Restrictions for Basic Operations

The following restrictions apply to the recommended basic operations to minimize implementation complexity:

**DropNamespace:** Only the `Restrict` behavior mode is required.
This means the namespace must be empty (no tables or child namespaces) before it can be dropped.
The `Cascade` behavior mode, which recursively drops all contents, is not required for basic implementations.

**DescribeTable:** Only `load_detailed_metadata=false` and `check_declared=false` (the defaults) are required.
This means the implementation only needs to return the table `location` without opening the dataset.
Returning detailed metadata such as `version`, `schema`, and `stats` (which require opening the dataset)
or checking whether the table exists only as a namespace declaration is not required for basic implementations.

### Why Not `CreateTable` and `DropTable`?

`CreateTable` and `DropTable` are common in most catalog systems,
but are intentionally excluded from the recommended basic operations because they involve
data operations that present challenges for catalog implementations:

- **Data Operation Complexity:**
  Both `CreateTable` and `DropTable` are data operations rather than pure metadata operations.
  They can be long-running, especially when dealing with large datasets or remote storage systems.
  This makes them difficult to implement reliably in catalog systems designed for fast metadata lookups.

- **Atomicity Guarantees:**
  Data operations require careful handling of atomicity. A failed `CreateTable` or `DropTable` operation
  can leave the system in an inconsistent state with partially created or deleted data files.
  Catalog implementations would need to implement complex cleanup and recovery mechanisms.

- **CreateTable Challenges:**
  `CreateTable` is particularly difficult for catalogs to fully implement because features like
  CREATE TABLE AS SELECT (CTAS) require either complicated staging mechanisms or multi-statement
  transaction support.

While some catalog systems can handle these complex workflows,
doing so typically requires deep, dedicated integration.
Lance Namespace aims to enable as many catalogs as possible to adopt Lance format. By focusing on
`DeclareTable` and `DeregisterTable` instead of `CreateTable` and `DropTable`, namespace implementations only
need to handle metadata operations that are simple, fast and atomic across all catalog solutions.
`CreateTable` and `DropTable` can then be fulfilled by combining these metadata operations with the Lance SDK.

## Operation Versioning

When a backwards incompatible change is introduced,
a new operation version needs to be created, with a naming convention of `<OperationId>V<version>`,
for example `ListNamespacesV2`, `DescribeTableV3`, etc.

## Request and Response Models

Each operation has a corresponding request and response model defined in the [Models](models/) section.
The naming convention is `<OperationId>Request` and `<OperationId>Response`.

For example:

- `CreateNamespaceRequest` / `CreateNamespaceResponse`
- `ListTablesRequest` / `ListTablesResponse`
- `DescribeTableRequest` / `DescribeTableResponse`

## Error Handling

All operations use a standardized error model with numeric error codes. 
Each operation documents the specific errors it may return.
See [Error Handling](errors.md) for the complete list of error codes and per-operation error documentation.



================================================
FILE: docs/src/namespace/operations/.pages
================================================
title: Operations
nav:
  - Overview: index.md
  - Errors: errors.md
  - models



================================================
FILE: docs/src/namespace/operations/models/AddColumnsEntry.md
================================================


# AddColumnsEntry


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**name** | **String** | Name of the new column |  |
|**expression** | **String** | SQL expression for the column (optional if virtual_column is specified) |  [optional] |
|**virtualColumn** | [**AddVirtualColumnEntry**](AddVirtualColumnEntry.md) |  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/AddVirtualColumnEntry.md
================================================


# AddVirtualColumnEntry


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**inputColumns** | **List&lt;String&gt;** | List of input Lance field paths for the virtual column. Nested fields use dot-separated segments; use backtick-quoted segments for literal dots and double backticks inside quoted segments. |  |
|**outputs** | [**List&lt;AddVirtualColumnOutputEntry&gt;**](AddVirtualColumnOutputEntry.md) | Output columns produced by the virtual column UDF |  |
|**image** | **String** | Docker image to use for the UDF |  |
|**udf** | **String** | Base64 encoded pickled UDF |  |
|**udfName** | **String** | Name of the UDF |  |
|**udfVersion** | **String** | Version of the UDF |  |
|**udfBackend** | **String** | UDF backend type (e.g. DockerUDFSpecV1) |  [optional] |
|**autoBackfill** | **Boolean** | Whether to automatically backfill the column after creation |  [optional] |
|**manifest** | **String** | JSON-serialized manifest for the UDF environment |  [optional] |
|**manifestChecksum** | **String** | SHA-256 checksum of the manifest content |  [optional] |
|**fieldMetadata** | **Map&lt;String, String&gt;** | User-supplied field metadata (string key-value pairs) |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/AddVirtualColumnOutputEntry.md
================================================


# AddVirtualColumnOutputEntry


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**column** | **String** | Physical output column name |  |
|**structField** | **String** | Field name in the UDF output struct |  |
|**dataType** | **Object** | Data type of the output column using JSON representation |  |
|**nullable** | **Boolean** | Whether the output column is nullable |  |
|**metadata** | **Map&lt;String, String&gt;** | User-supplied output field metadata (string key-value pairs) |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/AlterColumnsEntry.md
================================================


# AlterColumnsEntry


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**path** | **String** | Lance field path to alter. Nested fields use dot-separated segments; use backtick-quoted segments for literal dots and double backticks inside quoted segments. Use canonical full paths for display and errors; leaf names alone only identify top-level fields; invalid or unresolved paths should return InvalidInput or TableColumnNotFound. |  |
|**dataType** | **Object** | New data type for the column using JSON representation (optional) |  [optional] |
|**rename** | **String** | New name for the column (optional) |  [optional] |
|**nullable** | **Boolean** | Whether the column should be nullable (optional) |  [optional] |
|**virtualColumn** | [**AlterVirtualColumnEntry**](AlterVirtualColumnEntry.md) |  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/AlterTableAddColumnsRequest.md
================================================


# AlterTableAddColumnsRequest


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** | Table identifier path (namespace + table name) |  [optional] |
|**branch** | **String** | Branch to target. When not specified, the main branch is used.  |  [optional] |
|**newColumns** | [**List&lt;AddColumnsEntry&gt;**](AddColumnsEntry.md) | List of new columns to add to the table |  |






================================================
FILE: docs/src/namespace/operations/models/AlterTableAddColumnsResponse.md
================================================


# AlterTableAddColumnsResponse


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**version** | **Long** | The commit version associated with the operation |  |






================================================
FILE: docs/src/namespace/operations/models/AlterTableAlterColumnsRequest.md
================================================


# AlterTableAlterColumnsRequest


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** | Table identifier path (namespace + table name) |  [optional] |
|**branch** | **String** | Branch to target. When not specified, the main branch is used.  |  [optional] |
|**alterations** | [**List&lt;AlterColumnsEntry&gt;**](AlterColumnsEntry.md) | List of column alterations to apply to the table |  |






================================================
FILE: docs/src/namespace/operations/models/AlterTableAlterColumnsResponse.md
================================================


# AlterTableAlterColumnsResponse


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**version** | **Long** | The commit version associated with the operation |  |






================================================
FILE: docs/src/namespace/operations/models/AlterTableBackfillColumnsRequest.md
================================================


# AlterTableBackfillColumnsRequest


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** | Table identifier path (namespace + table name) |  [optional] |
|**branch** | **String** | Branch to target. When not specified, the main branch is used.  |  [optional] |
|**column** | **String** | Lance field path to backfill. Nested fields use dot-separated segments; use backtick-quoted segments for literal dots and double backticks inside quoted segments. Use canonical full paths for display and errors; leaf names alone only identify top-level fields; invalid or unresolved paths should return InvalidInput or TableColumnNotFound. |  |
|**where** | **String** | Optional WHERE clause filter |  [optional] |
|**concurrency** | **Integer** | Optional concurrency override |  [optional] |
|**intraApplierConcurrency** | **Integer** | Optional intra-applier concurrency override |  [optional] |
|**minCheckpointSize** | **Integer** | Optional minimum checkpoint size |  [optional] |
|**maxCheckpointSize** | **Integer** | Optional maximum checkpoint size |  [optional] |
|**batchCheckpointFlushIntervalSeconds** | **BigDecimal** | Optional batch checkpoint flush interval in seconds |  [optional] |
|**readVersion** | **Integer** | Optional table version to read from |  [optional] |
|**taskSize** | **Integer** | Optional task size |  [optional] |
|**numFrags** | **Integer** | Optional number of fragments |  [optional] |
|**checkpointSize** | **Integer** | Optional checkpoint size |  [optional] |
|**commitGranularity** | **Integer** | Optional commit granularity |  [optional] |
|**cluster** | **String** | Optional cluster name |  [optional] |
|**manifest** | **String** | Optional manifest name |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/AlterTableBackfillColumnsResponse.md
================================================


# AlterTableBackfillColumnsResponse


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**jobId** | **String** | The job ID for tracking the backfill job |  |






================================================
FILE: docs/src/namespace/operations/models/AlterTableDropColumnsRequest.md
================================================


# AlterTableDropColumnsRequest


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** |  |  [optional] |
|**branch** | **String** | Branch to target. When not specified, the main branch is used.  |  [optional] |
|**columns** | **List&lt;String&gt;** | Lance field paths to drop. Nested fields use dot-separated segments; use backtick-quoted segments for literal dots and double backticks inside quoted segments. Use canonical full paths for display and errors; leaf names alone only identify top-level fields; invalid or unresolved paths should return InvalidInput or TableColumnNotFound. |  |






================================================
FILE: docs/src/namespace/operations/models/AlterTableDropColumnsResponse.md
================================================


# AlterTableDropColumnsResponse


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**transactionId** | **String** | Optional transaction identifier |  [optional] |
|**version** | **Long** | Version of the table after dropping columns |  |






================================================
FILE: docs/src/namespace/operations/models/AlterTransactionAction.md
================================================


# AlterTransactionAction

A single action that could be performed to alter a transaction. This action holds the model definition for all types of specific actions models, this is to minimize difference and compatibility issue across codegen in different languages. When used, only one of the actions should be non-null for each action. If you would like to perform multiple actions, set a list of actions in the AlterTransactionRequest. 

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**setStatusAction** | [**AlterTransactionSetStatus**](AlterTransactionSetStatus.md) |  |  [optional] |
|**setPropertyAction** | [**AlterTransactionSetProperty**](AlterTransactionSetProperty.md) |  |  [optional] |
|**unsetPropertyAction** | [**AlterTransactionUnsetProperty**](AlterTransactionUnsetProperty.md) |  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/AlterTransactionRequest.md
================================================


# AlterTransactionRequest

Alter a transaction with a list of actions. The server should either succeed and apply all actions, or fail and apply no action. 

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** |  |  [optional] |
|**actions** | [**List&lt;AlterTransactionAction&gt;**](AlterTransactionAction.md) |  |  |






================================================
FILE: docs/src/namespace/operations/models/AlterTransactionResponse.md
================================================


# AlterTransactionResponse


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**status** | **String** | The status of a transaction. Case insensitive, supports both PascalCase and snake_case. Valid values are: - Queued: the transaction is queued and not yet started - Running: the transaction is currently running - Succeeded: the transaction has completed successfully - Failed: the transaction has failed - Canceled: the transaction was canceled  |  |
|**properties** | **Map&lt;String, String&gt;** |  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/AlterTransactionSetProperty.md
================================================


# AlterTransactionSetProperty


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**key** | **String** |  |  [optional] |
|**value** | **String** |  |  [optional] |
|**mode** | **String** | The behavior if the property key already exists. Case insensitive, supports both PascalCase and snake_case. Valid values are: - Overwrite (default): overwrite the existing value with the provided value - Fail: fail the entire operation - Skip: keep the existing value and skip setting the provided value  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/AlterTransactionSetStatus.md
================================================


# AlterTransactionSetStatus


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**status** | **String** | The status of a transaction. Case insensitive, supports both PascalCase and snake_case. Valid values are: - Queued: the transaction is queued and not yet started - Running: the transaction is currently running - Succeeded: the transaction has completed successfully - Failed: the transaction has failed - Canceled: the transaction was canceled  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/AlterTransactionUnsetProperty.md
================================================


# AlterTransactionUnsetProperty


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**key** | **String** |  |  [optional] |
|**mode** | **String** | The behavior if the property key to unset does not exist. Case insensitive, supports both PascalCase and snake_case. Valid values are: - Skip (default): skip the property to unset - Fail: fail the entire operation  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/AlterVirtualColumnEntry.md
================================================


# AlterVirtualColumnEntry


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**inputColumns** | **List&lt;String&gt;** | List of input Lance field paths for the virtual column. Nested fields use dot-separated segments; use backtick-quoted segments for literal dots and double backticks inside quoted segments. Optional. |  [optional] |
|**image** | **String** | Docker image to use for the UDF (optional) |  [optional] |
|**udf** | **String** | Base64 encoded pickled UDF (optional) |  [optional] |
|**udfName** | **String** | Name of the UDF (optional) |  [optional] |
|**udfVersion** | **String** | Version of the UDF (optional) |  [optional] |
|**udfBackend** | **String** | UDF backend type (e.g. DockerUDFSpecV1) (optional) |  [optional] |
|**autoBackfill** | **Boolean** | Whether to automatically backfill the column (optional) |  [optional] |
|**manifest** | **String** | JSON-serialized manifest for the UDF environment (optional) |  [optional] |
|**manifestChecksum** | **String** | SHA-256 checksum of the manifest content (optional) |  [optional] |
|**fieldMetadata** | **Map&lt;String, String&gt;** | User-supplied field metadata (optional) |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/AnalyzeTableQueryPlanRequest.md
================================================


# AnalyzeTableQueryPlanRequest


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** |  |  [optional] |
|**branch** | **String** | Branch to target. When not specified, the main branch is used.  |  [optional] |
|**bypassVectorIndex** | **Boolean** | Whether to bypass vector index |  [optional] |
|**columns** | [**QueryTableRequestColumns**](QueryTableRequestColumns.md) |  |  [optional] |
|**distanceType** | **String** | Distance metric to use |  [optional] |
|**ef** | **Integer** | Search effort parameter for HNSW index |  [optional] |
|**fastSearch** | **Boolean** | Whether to use fast search |  [optional] |
|**filter** | **String** | Optional SQL filter expression. Field references in the expression must use Lance field path syntax: nested fields use dot-separated segments, literal dots require backtick-quoted segments, and backticks inside quoted segments are doubled.  |  [optional] |
|**fullTextQuery** | [**QueryTableRequestFullTextQuery**](QueryTableRequestFullTextQuery.md) |  |  [optional] |
|**k** | **Integer** | Number of results to return |  |
|**lowerBound** | **Float** | Lower bound for search |  [optional] |
|**nprobes** | **Integer** | Number of probes for IVF index |  [optional] |
|**offset** | **Integer** | Number of results to skip |  [optional] |
|**prefilter** | **Boolean** | Whether to apply filtering before vector search |  [optional] |
|**refineFactor** | **Integer** | Refine factor for search |  [optional] |
|**upperBound** | **Float** | Upper bound for search |  [optional] |
|**vector** | [**QueryTableRequestVector**](QueryTableRequestVector.md) |  |  |
|**vectorColumn** | **String** | Lance field path of the vector field to search. Nested fields use dot-separated segments; use backtick-quoted segments for literal dots and double backticks inside quoted segments. Use canonical full paths for display and errors; leaf names alone only identify top-level fields; invalid or unresolved paths should return InvalidInput or TableColumnNotFound. |  [optional] |
|**version** | **Long** | Table version to query |  [optional] |
|**withRowId** | **Boolean** | If true, return the row id as a column called &#x60;_rowid&#x60; |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/AnalyzeTableQueryPlanResponse.md
================================================


# AnalyzeTableQueryPlanResponse


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**analysis** | **String** | Detailed analysis of the query execution plan |  |






================================================
FILE: docs/src/namespace/operations/models/BatchCommitTablesRequest.md
================================================


# BatchCommitTablesRequest

Request to atomically commit a batch of table operations. This replaces `BatchCreateTableVersionsRequest` with a more general interface that supports mixed operations (DeclareTable, CreateTableVersion, DeleteTableVersions, DeregisterTable) within a single atomic transaction at the metadata layer.  All operations are committed atomically: either all succeed or none are applied. 

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**operations** | [**List&lt;CommitTableOperation&gt;**](CommitTableOperation.md) | List of operations to commit atomically. Supported operation types: DeclareTable, CreateTableVersion, DeleteTableVersions, DeregisterTable.  |  |






================================================
FILE: docs/src/namespace/operations/models/BatchCommitTablesResponse.md
================================================


# BatchCommitTablesResponse

Response for a batch commit of table operations. Contains the results of each operation in the same order as the request. 

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**transactionId** | **String** | Optional transaction identifier for the batch commit |  [optional] |
|**results** | [**List&lt;CommitTableResult&gt;**](CommitTableResult.md) | Results for each operation, in the same order as the request operations. Each result contains the outcome of the corresponding operation.  |  |






================================================
FILE: docs/src/namespace/operations/models/BatchCreateTableVersionsRequest.md
================================================


# BatchCreateTableVersionsRequest

Request to atomically create new version entries for multiple tables. The operation is atomic: all versions are created or none are. 

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**entries** | [**List&lt;CreateTableVersionEntry&gt;**](CreateTableVersionEntry.md) | List of table version entries to create atomically |  |






================================================
FILE: docs/src/namespace/operations/models/BatchCreateTableVersionsResponse.md
================================================


# BatchCreateTableVersionsResponse

Response for batch creating table versions. Contains the created versions for each table in the same order as the request. 

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**transactionId** | **String** | Optional transaction identifier |  [optional] |
|**versions** | [**List&lt;TableVersion&gt;**](TableVersion.md) | List of created table versions in the same order as the request entries |  |






================================================
FILE: docs/src/namespace/operations/models/BatchDeleteTableVersionsRequest.md
================================================


# BatchDeleteTableVersionsRequest

Request to delete table version records. Supports deleting ranges of versions for efficient bulk cleanup. 

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** | The table identifier |  [optional] |
|**branch** | **String** | Branch to target. When not specified, the main branch is used.  |  [optional] |
|**ranges** | [**List&lt;VersionRange&gt;**](VersionRange.md) | List of version ranges to delete. Each range specifies start (inclusive) and end (exclusive) versions.  |  |






================================================
FILE: docs/src/namespace/operations/models/BatchDeleteTableVersionsResponse.md
================================================


# BatchDeleteTableVersionsResponse

Response for deleting table version records

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**deletedCount** | **Long** | Number of version records deleted |  [optional] |
|**transactionId** | **String** | Optional transaction identifier |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/BooleanQuery.md
================================================


# BooleanQuery

Boolean query with must, should, and must_not clauses

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**must** | [**List&lt;FtsQuery&gt;**](FtsQuery.md) | Queries that must match (AND) |  |
|**mustNot** | [**List&lt;FtsQuery&gt;**](FtsQuery.md) | Queries that must not match (NOT) |  |
|**should** | [**List&lt;FtsQuery&gt;**](FtsQuery.md) | Queries that should match (OR) |  |






================================================
FILE: docs/src/namespace/operations/models/BoostQuery.md
================================================


# BoostQuery

Boost query that scores documents matching positive query higher and negative query lower

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**positive** | [**FtsQuery**](FtsQuery.md) |  |  |
|**negative** | [**FtsQuery**](FtsQuery.md) |  |  |
|**negativeBoost** | **Float** | Boost factor for negative query (default: 0.5) |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/BranchApi.md
================================================
# BranchApi

All URIs are relative to *http://localhost:2333*

| Method | HTTP request | Description |
|------------- | ------------- | -------------|
| [**createTableBranch**](BranchApi.md#createTableBranch) | **POST** /v1/table/{id}/branches/create | Create a new branch |
| [**deleteTableBranch**](BranchApi.md#deleteTableBranch) | **POST** /v1/table/{id}/branches/delete | Delete a branch |
| [**listTableBranches**](BranchApi.md#listTableBranches) | **POST** /v1/table/{id}/branches/list | List all branches for a table |



## createTableBranch

> CreateTableBranchResponse createTableBranch(id, createTableBranchRequest, delimiter)

Create a new branch

Create a new branch for table &#x60;id&#x60; starting from a source ref (another branch and/or version), defaulting to the latest version of the main branch. 

### Example

```java
// Import classes:
import org.lance.namespace.client.apache.ApiClient;
import org.lance.namespace.client.apache.ApiException;
import org.lance.namespace.client.apache.Configuration;
import org.lance.namespace.client.apache.auth.*;
import org.lance.namespace.client.apache.models.*;
import org.lance.namespace.client.apache.api.BranchApi;

public class Example {
    public static void main(String[] args) {
        ApiClient defaultClient = Configuration.getDefaultApiClient();
        defaultClient.setBasePath("http://localhost:2333");
        
        // Configure OAuth2 access token for authorization: OAuth2
        OAuth OAuth2 = (OAuth) defaultClient.getAuthentication("OAuth2");
        OAuth2.setAccessToken("YOUR ACCESS TOKEN");

        // Configure API key authorization: ApiKeyAuth
        ApiKeyAuth ApiKeyAuth = (ApiKeyAuth) defaultClient.getAuthentication("ApiKeyAuth");
        ApiKeyAuth.setApiKey("YOUR API KEY");
        // Uncomment the following line to set a prefix for the API key, e.g. "Token" (defaults to null)
        //ApiKeyAuth.setApiKeyPrefix("Token");

        // Configure HTTP bearer authorization: BearerAuth
        HttpBearerAuth BearerAuth = (HttpBearerAuth) defaultClient.getAuthentication("BearerAuth");
        BearerAuth.setBearerToken("BEARER TOKEN");

        BranchApi apiInstance = new BranchApi(defaultClient);
        String id = "id_example"; // String | `string identifier` of an object in a namespace, following the Lance Namespace spec. When the value is equal to the delimiter, it represents the root namespace. For example, `v1/namespace/$/list` performs a `ListNamespace` on the root namespace. 
        CreateTableBranchRequest createTableBranchRequest = new CreateTableBranchRequest(); // CreateTableBranchRequest | 
        String delimiter = "delimiter_example"; // String | An optional delimiter of the `string identifier`, following the Lance Namespace spec. When not specified, the `$` delimiter must be used. 
        try {
            CreateTableBranchResponse result = apiInstance.createTableBranch(id, createTableBranchRequest, delimiter);
            System.out.println(result);
        } catch (ApiException e) {
            System.err.println("Exception when calling BranchApi#createTableBranch");
            System.err.println("Status code: " + e.getCode());
            System.err.println("Reason: " + e.getResponseBody());
            System.err.println("Response headers: " + e.getResponseHeaders());
            e.printStackTrace();
        }
    }
}
```

### Parameters


| Name | Type | Description  | Notes |
|------------- | ------------- | ------------- | -------------|
| **id** | **String**| &#x60;string identifier&#x60; of an object in a namespace, following the Lance Namespace spec. When the value is equal to the delimiter, it represents the root namespace. For example, &#x60;v1/namespace/$/list&#x60; performs a &#x60;ListNamespace&#x60; on the root namespace.  | |
| **createTableBranchRequest** | [**CreateTableBranchRequest**](CreateTableBranchRequest.md)|  | |
| **delimiter** | **String**| An optional delimiter of the &#x60;string identifier&#x60;, following the Lance Namespace spec. When not specified, the &#x60;$&#x60; delimiter must be used.  | [optional] |

### Return type

[**CreateTableBranchResponse**](CreateTableBranchResponse.md)

### Authorization

[OAuth2](../README.md#OAuth2), [ApiKeyAuth](../README.md#ApiKeyAuth), [BearerAuth](../README.md#BearerAuth)

### HTTP request headers

- **Content-Type**: application/json
- **Accept**: application/json


### HTTP response details
| Status code | Description | Response headers |
|-------------|-------------|------------------|
| **200** | Create branch response |  -  |
| **400** | Indicates a bad request error. It could be caused by an unexpected request body format or other forms of request validation failure, such as invalid json. Usually serves application/json content, although in some cases simple text/plain content might be returned by the server&#39;s middleware. |  -  |
| **401** | Unauthorized. The request lacks valid authentication credentials for the operation. |  -  |
| **403** | Forbidden. Authenticated user does not have the necessary permissions. |  -  |
| **404** | A server-side problem that means can not find the specified resource. |  -  |
| **409** | The request conflicts with the current state of the target resource. |  -  |
| **503** | The service is not ready to handle the request. The client should wait and retry. The service may additionally send a Retry-After header to indicate when to retry. |  -  |
| **5XX** | A server-side problem that might not be addressable from the client side. Used for server 5xx errors without more specific documentation in individual routes. |  -  |


## deleteTableBranch

> DeleteTableBranchResponse deleteTableBranch(id, deleteTableBranchRequest, delimiter)

Delete a branch

Delete an existing branch from table &#x60;id&#x60;. 

### Example

```java
// Import classes:
import org.lance.namespace.client.apache.ApiClient;
import org.lance.namespace.client.apache.ApiException;
import org.lance.namespace.client.apache.Configuration;
import org.lance.namespace.client.apache.auth.*;
import org.lance.namespace.client.apache.models.*;
import org.lance.namespace.client.apache.api.BranchApi;

public class Example {
    public static void main(String[] args) {
        ApiClient defaultClient = Configuration.getDefaultApiClient();
        defaultClient.setBasePath("http://localhost:2333");
        
        // Configure OAuth2 access token for authorization: OAuth2
        OAuth OAuth2 = (OAuth) defaultClient.getAuthentication("OAuth2");
        OAuth2.setAccessToken("YOUR ACCESS TOKEN");

        // Configure API key authorization: ApiKeyAuth
        ApiKeyAuth ApiKeyAuth = (ApiKeyAuth) defaultClient.getAuthentication("ApiKeyAuth");
        ApiKeyAuth.setApiKey("YOUR API KEY");
        // Uncomment the following line to set a prefix for the API key, e.g. "Token" (defaults to null)
        //ApiKeyAuth.setApiKeyPrefix("Token");

        // Configure HTTP bearer authorization: BearerAuth
        HttpBearerAuth BearerAuth = (HttpBearerAuth) defaultClient.getAuthentication("BearerAuth");
        BearerAuth.setBearerToken("BEARER TOKEN");

        BranchApi apiInstance = new BranchApi(defaultClient);
        String id = "id_example"; // String | `string identifier` of an object in a namespace, following the Lance Namespace spec. When the value is equal to the delimiter, it represents the root namespace. For example, `v1/namespace/$/list` performs a `ListNamespace` on the root namespace. 
        DeleteTableBranchRequest deleteTableBranchRequest = new DeleteTableBranchRequest(); // DeleteTableBranchRequest | 
        String delimiter = "delimiter_example"; // String | An optional delimiter of the `string identifier`, following the Lance Namespace spec. When not specified, the `$` delimiter must be used. 
        try {
            DeleteTableBranchResponse result = apiInstance.deleteTableBranch(id, deleteTableBranchRequest, delimiter);
            System.out.println(result);
        } catch (ApiException e) {
            System.err.println("Exception when calling BranchApi#deleteTableBranch");
            System.err.println("Status code: " + e.getCode());
            System.err.println("Reason: " + e.getResponseBody());
            System.err.println("Response headers: " + e.getResponseHeaders());
            e.printStackTrace();
        }
    }
}
```

### Parameters


| Name | Type | Description  | Notes |
|------------- | ------------- | ------------- | -------------|
| **id** | **String**| &#x60;string identifier&#x60; of an object in a namespace, following the Lance Namespace spec. When the value is equal to the delimiter, it represents the root namespace. For example, &#x60;v1/namespace/$/list&#x60; performs a &#x60;ListNamespace&#x60; on the root namespace.  | |
| **deleteTableBranchRequest** | [**DeleteTableBranchRequest**](DeleteTableBranchRequest.md)|  | |
| **delimiter** | **String**| An optional delimiter of the &#x60;string identifier&#x60;, following the Lance Namespace spec. When not specified, the &#x60;$&#x60; delimiter must be used.  | [optional] |

### Return type

[**DeleteTableBranchResponse**](DeleteTableBranchResponse.md)

### Authorization

[OAuth2](../README.md#OAuth2), [ApiKeyAuth](../README.md#ApiKeyAuth), [BearerAuth](../README.md#BearerAuth)

### HTTP request headers

- **Content-Type**: application/json
- **Accept**: application/json


### HTTP response details
| Status code | Description | Response headers |
|-------------|-------------|------------------|
| **200** | Delete branch response |  -  |
| **400** | Indicates a bad request error. It could be caused by an unexpected request body format or other forms of request validation failure, such as invalid json. Usually serves application/json content, although in some cases simple text/plain content might be returned by the server&#39;s middleware. |  -  |
| **401** | Unauthorized. The request lacks valid authentication credentials for the operation. |  -  |
| **403** | Forbidden. Authenticated user does not have the necessary permissions. |  -  |
| **404** | A server-side problem that means can not find the specified resource. |  -  |
| **503** | The service is not ready to handle the request. The client should wait and retry. The service may additionally send a Retry-After header to indicate when to retry. |  -  |
| **5XX** | A server-side problem that might not be addressable from the client side. Used for server 5xx errors without more specific documentation in individual routes. |  -  |


## listTableBranches

> ListTableBranchesResponse listTableBranches(id, delimiter, pageToken, limit)

List all branches for a table

List all branches that have been created for table &#x60;id&#x60;. Returns a map of branch names to their contents.  REST NAMESPACE ONLY REST namespace does not use a request body for this operation. The &#x60;ListTableBranchesRequest&#x60; information is passed in the following way: - &#x60;id&#x60;: pass through path parameter of the same name - &#x60;page_token&#x60;: pass through query parameter of the same name - &#x60;limit&#x60;: pass through query parameter of the same name 

### Example

```java
// Import classes:
import org.lance.namespace.client.apache.ApiClient;
import org.lance.namespace.client.apache.ApiException;
import org.lance.namespace.client.apache.Configuration;
import org.lance.namespace.client.apache.auth.*;
import org.lance.namespace.client.apache.models.*;
import org.lance.namespace.client.apache.api.BranchApi;

public class Example {
    public static void main(String[] args) {
        ApiClient defaultClient = Configuration.getDefaultApiClient();
        defaultClient.setBasePath("http://localhost:2333");
        
        // Configure OAuth2 access token for authorization: OAuth2
        OAuth OAuth2 = (OAuth) defaultClient.getAuthentication("OAuth2");
        OAuth2.setAccessToken("YOUR ACCESS TOKEN");

        // Configure API key authorization: ApiKeyAuth
        ApiKeyAuth ApiKeyAuth = (ApiKeyAuth) defaultClient.getAuthentication("ApiKeyAuth");
        ApiKeyAuth.setApiKey("YOUR API KEY");
        // Uncomment the following line to set a prefix for the API key, e.g. "Token" (defaults to null)
        //ApiKeyAuth.setApiKeyPrefix("Token");

        // Configure HTTP bearer authorization: BearerAuth
        HttpBearerAuth BearerAuth = (HttpBearerAuth) defaultClient.getAuthentication("BearerAuth");
        BearerAuth.setBearerToken("BEARER TOKEN");

        BranchApi apiInstance = new BranchApi(defaultClient);
        String id = "id_example"; // String | `string identifier` of an object in a namespace, following the Lance Namespace spec. When the value is equal to the delimiter, it represents the root namespace. For example, `v1/namespace/$/list` performs a `ListNamespace` on the root namespace. 
        String delimiter = "delimiter_example"; // String | An optional delimiter of the `string identifier`, following the Lance Namespace spec. When not specified, the `$` delimiter must be used. 
        String pageToken = "pageToken_example"; // String | Pagination token from a previous request
        Integer limit = 56; // Integer | Maximum number of items to return
        try {
            ListTableBranchesResponse result = apiInstance.listTableBranches(id, delimiter, pageToken, limit);
            System.out.println(result);
        } catch (ApiException e) {
            System.err.println("Exception when calling BranchApi#listTableBranches");
            System.err.println("Status code: " + e.getCode());
            System.err.println("Reason: " + e.getResponseBody());
            System.err.println("Response headers: " + e.getResponseHeaders());
            e.printStackTrace();
        }
    }
}
```

### Parameters


| Name | Type | Description  | Notes |
|------------- | ------------- | ------------- | -------------|
| **id** | **String**| &#x60;string identifier&#x60; of an object in a namespace, following the Lance Namespace spec. When the value is equal to the delimiter, it represents the root namespace. For example, &#x60;v1/namespace/$/list&#x60; performs a &#x60;ListNamespace&#x60; on the root namespace.  | |
| **delimiter** | **String**| An optional delimiter of the &#x60;string identifier&#x60;, following the Lance Namespace spec. When not specified, the &#x60;$&#x60; delimiter must be used.  | [optional] |
| **pageToken** | **String**| Pagination token from a previous request | [optional] |
| **limit** | **Integer**| Maximum number of items to return | [optional] |

### Return type

[**ListTableBranchesResponse**](ListTableBranchesResponse.md)

### Authorization

[OAuth2](../README.md#OAuth2), [ApiKeyAuth](../README.md#ApiKeyAuth), [BearerAuth](../README.md#BearerAuth)

### HTTP request headers

- **Content-Type**: Not defined
- **Accept**: application/json


### HTTP response details
| Status code | Description | Response headers |
|-------------|-------------|------------------|
| **200** | List of table branches |  -  |
| **400** | Indicates a bad request error. It could be caused by an unexpected request body format or other forms of request validation failure, such as invalid json. Usually serves application/json content, although in some cases simple text/plain content might be returned by the server&#39;s middleware. |  -  |
| **401** | Unauthorized. The request lacks valid authentication credentials for the operation. |  -  |
| **403** | Forbidden. Authenticated user does not have the necessary permissions. |  -  |
| **404** | A server-side problem that means can not find the specified resource. |  -  |
| **503** | The service is not ready to handle the request. The client should wait and retry. The service may additionally send a Retry-After header to indicate when to retry. |  -  |
| **5XX** | A server-side problem that might not be addressable from the client side. Used for server 5xx errors without more specific documentation in individual routes. |  -  |




================================================
FILE: docs/src/namespace/operations/models/BranchContents.md
================================================


# BranchContents


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**parentBranch** | **String** | Name of the branch this branch was created from. Absent when the branch was created from the main branch.  |  [optional] |
|**parentVersion** | **Long** | Version of the parent (branch or main) this branch was created from |  |
|**createAt** | **Long** | Unix timestamp (in seconds) when the branch was created |  |
|**manifestSize** | **Long** | Size of the branch&#39;s manifest file in bytes |  |
|**metadata** | **Map&lt;String, String&gt;** | Key-value metadata associated with the branch |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/CommitTableOperation.md
================================================


# CommitTableOperation

A single operation within a batch commit. Provide exactly one of the operation fields to specify the operation kind. 

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**declareTable** | [**DeclareTableRequest**](DeclareTableRequest.md) | Declare (reserve) a new table in the namespace |  [optional] |
|**createTableVersion** | [**CreateTableVersionRequest**](CreateTableVersionRequest.md) | Create a new version entry for a table |  [optional] |
|**deleteTableVersions** | [**BatchDeleteTableVersionsRequest**](BatchDeleteTableVersionsRequest.md) | Delete version ranges from a table |  [optional] |
|**deregisterTable** | [**DeregisterTableRequest**](DeregisterTableRequest.md) | Deregister (soft-delete) a table |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/CommitTableResult.md
================================================


# CommitTableResult

Result of a single operation within a batch commit. Each result corresponds to one operation in the request, in the same order. Exactly one of the result fields will be set. 

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**declareTable** | [**DeclareTableResponse**](DeclareTableResponse.md) | Result of a DeclareTable operation |  [optional] |
|**createTableVersion** | [**CreateTableVersionResponse**](CreateTableVersionResponse.md) | Result of a CreateTableVersion operation |  [optional] |
|**deleteTableVersions** | [**BatchDeleteTableVersionsResponse**](BatchDeleteTableVersionsResponse.md) | Result of a DeleteTableVersions operation |  [optional] |
|**deregisterTable** | [**DeregisterTableResponse**](DeregisterTableResponse.md) | Result of a DeregisterTable operation |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/CountTableRowsRequest.md
================================================


# CountTableRowsRequest


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** |  |  [optional] |
|**version** | **Long** | Version of the table to describe. If not specified, server should resolve it to the latest version.  |  [optional] |
|**branch** | **String** | Branch to target. When not specified, the main branch is used.  |  [optional] |
|**predicate** | **String** | Optional SQL predicate to filter rows for counting. Field references must use Lance field path syntax: nested fields use dot-separated segments, literal dots require backtick-quoted segments, and backticks inside quoted segments are doubled.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/CountTableRowsResponse.md
================================================


# CountTableRowsResponse

Response containing the count of rows.  The REST namespace does not transmit this object directly (see the CountTableRows operation for how the bare-number response maps to it). It is the standard data model for the LanceNamespace interfaces (e.g. Java, Python). 

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**count** | **Long** | The count of rows. |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/CreateMaterializedViewRequest.md
================================================


# CreateMaterializedViewRequest


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** | View identifier path (namespace + view name) |  [optional] |
|**kind** | [**KindEnum**](#KindEnum) | The materialized view kind. - &#x60;query&#x60; — plain query-backed view (no UDTF), 1:1 rows. - &#x60;udtf&#x60; — batch UDTF-backed view (N:M rows, full refresh). - &#x60;chunker&#x60;, aka &#39;scalar_udtf&#39; — chunker view (1:N row expansion, incremental refresh).  |  |
|**sourceQuery** | **String** | Opaque serialized representation of the source query that defines the view&#39;s input. The format is defined by the client; the namespace server stores it without interpreting it.  |  |
|**outputSchema** | **String** | Base64-encoded Arrow schema of the view output |  |
|**udtfSpec** | [**MaterializedViewUdtfEntry**](MaterializedViewUdtfEntry.md) |  |  [optional] |
|**withNoData** | **Boolean** | If false, the server kicks off an initial refresh immediately after creating the view and the response includes a job ID.  |  [optional] |
|**autoRefresh** | **Boolean** | If true, the view is automatically refreshed when source-table data changes past the deployment-level threshold. Boolean opt-in only; the threshold and cooldown are configured on the deployment, not per-view.  |  [optional] |



## Enum: KindEnum

| Name | Value |
|---- | -----|
| QUERY | &quot;query&quot; |
| UDTF | &quot;udtf&quot; |
| CHUNKER | &quot;chunker&quot; |






================================================
FILE: docs/src/namespace/operations/models/CreateMaterializedViewResponse.md
================================================


# CreateMaterializedViewResponse


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**version** | **Long** | The commit version that created the materialized view |  |
|**jobId** | **String** | Refresh job ID, populated only when &#x60;with_no_data&#x60; was false.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/CreateNamespaceRequest.md
================================================


# CreateNamespaceRequest


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** |  |  [optional] |
|**mode** | **String** | There are three modes when trying to create a namespace, to differentiate the behavior when a namespace of the same name already exists. Case insensitive, supports both PascalCase and snake_case. Valid values are:   * Create: the operation fails with 409.   * ExistOk: the operation succeeds and the existing namespace is kept.   * Overwrite: the existing namespace is dropped and a new empty namespace with this name is created.  |  [optional] |
|**properties** | **Map&lt;String, String&gt;** | Properties stored on the namespace, if supported by the implementation.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/CreateNamespaceResponse.md
================================================


# CreateNamespaceResponse


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**transactionId** | **String** | Optional transaction identifier |  [optional] |
|**properties** | **Map&lt;String, String&gt;** | Properties after the namespace is created.  If the server does not support namespace properties, it should return null for this field. If namespace properties are supported, but none are set, it should return an empty object.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/CreateTableBranchRequest.md
================================================


# CreateTableBranchRequest


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** |  |  [optional] |
|**name** | **String** | Name of the branch to create |  |
|**fromBranch** | **String** | Source branch to create the new branch from. When omitted, the new branch is created from the main branch.  |  [optional] |
|**fromVersion** | **Long** | Version of the source (branch or main) to create from. When omitted, the latest version of the source is used.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/CreateTableBranchResponse.md
================================================


# CreateTableBranchResponse

Response for create branch operation

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**transactionId** | **String** | Optional transaction identifier |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/CreateTableIndexRequest.md
================================================


# CreateTableIndexRequest


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** |  |  [optional] |
|**branch** | **String** | Branch to target. When not specified, the main branch is used.  |  [optional] |
|**column** | **String** | Lance field path to create the index on. Nested fields use dot-separated segments; use backtick-quoted segments for literal dots and double backticks inside quoted segments. Use canonical full paths for display and errors; leaf names alone only identify top-level fields; invalid or unresolved paths should return InvalidInput or TableColumnNotFound. |  |
|**indexType** | **String** | Type of index to create (e.g., BTREE, BITMAP, LABEL_LIST, IVF_FLAT, IVF_PQ, IVF_HNSW_SQ, FTS) |  |
|**name** | **String** | Optional name for the index. If not provided, a name will be auto-generated. |  [optional] |
|**distanceType** | **String** | Distance metric type for vector indexes (e.g., l2, cosine, dot) |  [optional] |
|**withPosition** | **Boolean** | Optional FTS parameter for position tracking |  [optional] |
|**baseTokenizer** | **String** | Optional FTS parameter for base tokenizer |  [optional] |
|**language** | **String** | Optional FTS parameter for language |  [optional] |
|**maxTokenLength** | **Integer** | Optional FTS parameter for maximum token length |  [optional] |
|**lowerCase** | **Boolean** | Optional FTS parameter for lowercase conversion |  [optional] |
|**stem** | **Boolean** | Optional FTS parameter for stemming |  [optional] |
|**removeStopWords** | **Boolean** | Optional FTS parameter for stop word removal |  [optional] |
|**asciiFolding** | **Boolean** | Optional FTS parameter for ASCII folding |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/CreateTableIndexResponse.md
================================================


# CreateTableIndexResponse

Response for create index operation

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**transactionId** | **String** | Optional transaction identifier |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/CreateTableRequest.md
================================================


# CreateTableRequest

Request for creating a table, excluding the Arrow IPC stream. The table location and any credential vending behavior are determined by the implementation and returned in the response, rather than specified in this request. 

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** |  |  [optional] |
|**mode** | **String** | There are three modes when trying to create a table, to differentiate the behavior when a table of the same name already exists. Case insensitive, supports both PascalCase and snake_case. Valid values are:   * Create: the operation fails with 409.   * ExistOk: the operation succeeds and the existing table is kept.   * Overwrite: the existing table is dropped and a new table with this name is created.  |  [optional] |
|**properties** | **Map&lt;String, String&gt;** | Business logic properties stored and managed by the namespace implementation outside Lance context, if supported by the implementation.  |  [optional] |
|**storageOptions** | **Map&lt;String, String&gt;** | Storage options that configure overrides for writing table data and metadata during table creation. These are passed to Lance for the write path.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/CreateTableResponse.md
================================================


# CreateTableResponse


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**transactionId** | **String** | Optional transaction identifier |  [optional] |
|**location** | **String** |  |  [optional] |
|**version** | **Long** |  |  [optional] |
|**storageOptions** | **Map&lt;String, String&gt;** | Configuration options to be used to access storage. The available options depend on the type of storage in use. These will be passed directly to Lance to initialize storage access.  |  [optional] |
|**properties** | **Map&lt;String, String&gt;** | Business logic properties stored and managed by the namespace implementation outside Lance context. If the implementation does not support table properties, it should return null for this field.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/CreateTableScalarIndexResponse.md
================================================


# CreateTableScalarIndexResponse

Response for create scalar index operation

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**transactionId** | **String** | Optional transaction identifier |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/CreateTableTagRequest.md
================================================


# CreateTableTagRequest


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** |  |  [optional] |
|**tag** | **String** | Name of the tag to create |  |
|**version** | **Long** | Version number for the tag to point to |  |
|**branch** | **String** | Branch to target. When not specified, the main branch is used.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/CreateTableTagResponse.md
================================================


# CreateTableTagResponse

Response for create tag operation

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**transactionId** | **String** | Optional transaction identifier |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/CreateTableVersionEntry.md
================================================


# CreateTableVersionEntry

An entry for creating a new table version in a batch operation. This supports `put_if_not_exists` semantics, where the operation fails if the version already exists. 

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**id** | **List&lt;String&gt;** | The table identifier |  |
|**version** | **Long** | Version number to create |  |
|**branch** | **String** | Branch to target. When not specified, the main branch is used.  |  [optional] |
|**manifestPath** | **String** | Path to the manifest file for this version |  |
|**manifestSize** | **Long** | Size of the manifest file in bytes |  [optional] |
|**eTag** | **String** | Optional ETag for the manifest file |  [optional] |
|**metadata** | **Map&lt;String, String&gt;** | Optional metadata for the version |  [optional] |
|**namingScheme** | **String** | The naming scheme used for manifest files in the &#x60;_versions/&#x60; directory.  Known values: - &#x60;V1&#x60;: &#x60;_versions/{version}.manifest&#x60; - Simple version-based naming - &#x60;V2&#x60;: &#x60;_versions/{inverted_version}.manifest&#x60; - Zero-padded, reversed version number   (uses &#x60;u64::MAX - version&#x60;) for O(1) lookup of latest version on object stores  V2 is preferred for new tables as it enables efficient latest-version discovery without needing to list all versions.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/CreateTableVersionRequest.md
================================================


# CreateTableVersionRequest

Request to create a new table version entry. This supports `put_if_not_exists` semantics, where the operation fails if the version already exists. 

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** | The table identifier |  [optional] |
|**version** | **Long** | Version number to create |  |
|**branch** | **String** | Branch to target. When not specified, the main branch is used.  |  [optional] |
|**manifestPath** | **String** | Path to the manifest file for this version |  |
|**manifestSize** | **Long** | Size of the manifest file in bytes |  [optional] |
|**eTag** | **String** | Optional ETag for the manifest file |  [optional] |
|**metadata** | **Map&lt;String, String&gt;** | Optional metadata for the version |  [optional] |
|**namingScheme** | **String** | The naming scheme used for manifest files in the &#x60;_versions/&#x60; directory.  Known values: - &#x60;V1&#x60;: &#x60;_versions/{version}.manifest&#x60; - Simple version-based naming - &#x60;V2&#x60;: &#x60;_versions/{inverted_version}.manifest&#x60; - Zero-padded, reversed version number   (uses &#x60;u64::MAX - version&#x60;) for O(1) lookup of latest version on object stores  V2 is preferred for new tables as it enables efficient latest-version discovery without needing to list all versions.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/CreateTableVersionResponse.md
================================================


# CreateTableVersionResponse

Response for creating a table version

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**transactionId** | **String** | Optional transaction identifier |  [optional] |
|**version** | [**TableVersion**](TableVersion.md) |  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/DeclareTableRequest.md
================================================


# DeclareTableRequest

Request for declaring a table. 

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** |  |  [optional] |
|**location** | **String** | Optional storage location for the table. If not provided, the namespace implementation should determine the table location.  |  [optional] |
|**vendCredentials** | **Boolean** | Whether to include vended credentials in the response &#x60;storage_options&#x60;. When true, the implementation should provide vended credentials for accessing storage. When not set, the implementation can decide whether to return vended credentials.  |  [optional] |
|**properties** | **Map&lt;String, String&gt;** | Business logic properties stored and managed by the namespace implementation outside Lance context, if supported by the implementation.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/DeclareTableResponse.md
================================================


# DeclareTableResponse

Response for declaring a table. 

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**transactionId** | **String** | Optional transaction identifier |  [optional] |
|**location** | **String** |  |  [optional] |
|**storageOptions** | **Map&lt;String, String&gt;** | Configuration options to be used to access storage. The available options depend on the type of storage in use. These will be passed directly to Lance to initialize storage access.  |  [optional] |
|**properties** | **Map&lt;String, String&gt;** | If the implementation does not support table properties, it should return null for this field. Otherwise it should return the properties.  |  [optional] |
|**managedVersioning** | **Boolean** | When true, the caller should use namespace table version operations (CreateTableVersion, BatchCreateTableVersions, DescribeTableVersion, ListTableVersions, BatchDeleteTableVersions) to manage table versions instead of relying on Lance&#39;s native version management.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/DeleteFromTableRequest.md
================================================


# DeleteFromTableRequest

Delete data from table based on a SQL predicate. Returns the number of rows that were deleted. 

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** | The namespace identifier |  [optional] |
|**branch** | **String** | Branch to target. When not specified, the main branch is used.  |  [optional] |
|**predicate** | **String** | SQL predicate to filter rows for deletion. Field references must use Lance field path syntax: nested fields use dot-separated segments, literal dots require backtick-quoted segments, and backticks inside quoted segments are doubled. |  |






================================================
FILE: docs/src/namespace/operations/models/DeleteFromTableResponse.md
================================================


# DeleteFromTableResponse


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**transactionId** | **String** | Optional transaction identifier |  [optional] |
|**version** | **Long** | The commit version associated with the operation |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/DeleteTableBranchRequest.md
================================================


# DeleteTableBranchRequest


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** |  |  [optional] |
|**name** | **String** | Name of the branch to delete |  |






================================================
FILE: docs/src/namespace/operations/models/DeleteTableBranchResponse.md
================================================


# DeleteTableBranchResponse

Response for delete branch operation

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**transactionId** | **String** | Optional transaction identifier |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/DeleteTableTagRequest.md
================================================


# DeleteTableTagRequest


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** |  |  [optional] |
|**tag** | **String** | Name of the tag to delete |  |






================================================
FILE: docs/src/namespace/operations/models/DeleteTableTagResponse.md
================================================


# DeleteTableTagResponse

Response for delete tag operation

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**transactionId** | **String** | Optional transaction identifier |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/DeregisterTableRequest.md
================================================


# DeregisterTableRequest

The table content remains available in the storage. 

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** |  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/DeregisterTableResponse.md
================================================


# DeregisterTableResponse


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**transactionId** | **String** | Optional transaction identifier |  [optional] |
|**id** | **List&lt;String&gt;** |  |  [optional] |
|**location** | **String** |  |  [optional] |
|**properties** | **Map&lt;String, String&gt;** | If the implementation does not support table properties, it should return null for this field. Otherwise it should return the properties.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/DescribeNamespaceRequest.md
================================================


# DescribeNamespaceRequest


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** |  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/DescribeNamespaceResponse.md
================================================


# DescribeNamespaceResponse


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**properties** | **Map&lt;String, String&gt;** | Properties stored on the namespace, if supported by the server. If the server does not support namespace properties, it should return null for this field. If namespace properties are supported, but none are set, it should return an empty object. |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/DescribeTableIndexStatsRequest.md
================================================


# DescribeTableIndexStatsRequest


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** |  |  [optional] |
|**version** | **Long** | Optional table version to get stats for |  [optional] |
|**branch** | **String** | Branch to target. When not specified, the main branch is used.  |  [optional] |
|**indexName** | **String** | Name of the index |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/DescribeTableIndexStatsResponse.md
================================================


# DescribeTableIndexStatsResponse


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**distanceType** | **String** | Distance type for vector indexes |  [optional] |
|**indexType** | **String** | Type of the index |  [optional] |
|**numIndexedRows** | **Long** | Number of indexed rows |  [optional] |
|**numUnindexedRows** | **Long** | Number of unindexed rows |  [optional] |
|**numIndices** | **Integer** | Number of indices |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/DescribeTableRequest.md
================================================


# DescribeTableRequest


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** |  |  [optional] |
|**version** | **Long** | Version of the table to describe. If not specified, server should resolve it to the latest version.  |  [optional] |
|**tag** | **String** | Tag name to describe the table at. If specified, the server should resolve the tag to a version number and describe that version. Cannot be used together with &#x60;version&#x60; or &#x60;branch&#x60;.  |  [optional] |
|**branch** | **String** | Branch to target. When not specified, the main branch is used.  |  [optional] |
|**withTableUri** | **Boolean** | Whether to include the table URI in the response. Default is false.  |  [optional] |
|**loadDetailedMetadata** | **Boolean** | Whether to load detailed metadata that requires opening the dataset. When true, the response must include all detailed metadata such as &#x60;version&#x60;, &#x60;schema&#x60;, and &#x60;stats&#x60; which require reading the dataset. When not set, the implementation can decide whether to return detailed metadata and which parts of detailed metadata to return.  |  [optional] |
|**checkDeclared** | **Boolean** | Whether to check if the table exists only as a namespace declaration without storage data. Default is false. When true, the response should populate &#x60;is_only_declared&#x60;. When false, the implementation should return null for &#x60;is_only_declared&#x60; unless another option such as &#x60;load_detailed_metadata&#x60; requires checking declared-only table state.  |  [optional] |
|**vendCredentials** | **Boolean** | Whether to include vended credentials in the response &#x60;storage_options&#x60;. When true, the implementation should provide vended credentials for accessing storage. When not set, the implementation can decide whether to return vended credentials.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/DescribeTableResponse.md
================================================


# DescribeTableResponse


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**table** | **String** | Table name. Only populated when &#x60;load_detailed_metadata&#x60; is true.  |  [optional] |
|**namespace** | **List&lt;String&gt;** | The namespace identifier as a list of parts. Only populated when &#x60;load_detailed_metadata&#x60; is true.  |  [optional] |
|**version** | **Long** | Table version number. Only populated when &#x60;load_detailed_metadata&#x60; is true.  |  [optional] |
|**location** | **String** | Table storage location (e.g., S3/GCS path).  |  [optional] |
|**tableUri** | **String** | Table URI. Unlike location, this field must be a complete and valid URI. Only returned when &#x60;with_table_uri&#x60; is true.  |  [optional] |
|**schema** | [**JsonArrowSchema**](JsonArrowSchema.md) | Table schema in JSON Arrow format. Only populated when &#x60;load_detailed_metadata&#x60; is true.  |  [optional] |
|**storageOptions** | **Map&lt;String, String&gt;** | Configuration options to be used to access storage. The available options depend on the type of storage in use. These will be passed directly to Lance to initialize storage access. When &#x60;vend_credentials&#x60; is true, this field may include vended credentials. If the vended credentials are temporary, the &#x60;expires_at_millis&#x60; key should be included to indicate the millisecond timestamp when the credentials expire.  |  [optional] |
|**stats** | [**TableBasicStats**](TableBasicStats.md) | Table statistics. Only populated when &#x60;load_detailed_metadata&#x60; is true.  |  [optional] |
|**metadata** | **Map&lt;String, String&gt;** | Optional table metadata as key-value pairs. This records the information of the table and requires loading the table. It is only populated when &#x60;load_detailed_metadata&#x60; is true.  |  [optional] |
|**properties** | **Map&lt;String, String&gt;** | Properties stored on the table, if supported by the server. This records the information managed by the namespace. If the server does not support table properties, it should return null for this field. If table properties are supported, but none are set, it should return an empty object. |  [optional] |
|**managedVersioning** | **Boolean** | When true, the caller should use namespace table version operations (CreateTableVersion, BatchCreateTableVersions, DescribeTableVersion, ListTableVersions, BatchDeleteTableVersions) to manage table versions instead of relying on Lance&#39;s native version management.  |  [optional] |
|**isOnlyDeclared** | **Boolean** | When true, indicates that the table has been declared in the namespace but not yet created on storage. This means the table exists in the namespace but has no data files on the underlying storage. When false, the table has storage components (data and metadata files). When null, the implementation did not check whether the table is only declared. Clients should treat an omitted value as null. Implementations should populate this field when &#x60;check_declared&#x60; is true or another option such as &#x60;load_detailed_metadata&#x60; requires checking declared-only table state. Operations like describe_table with load_detailed_metadata&#x3D;true may fail for declared-only tables.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/DescribeTableVersionRequest.md
================================================


# DescribeTableVersionRequest

Request to describe a specific table version

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** | The table identifier |  [optional] |
|**version** | **Long** | Version number to describe |  [optional] |
|**branch** | **String** | Branch to target. When not specified, the main branch is used.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/DescribeTableVersionResponse.md
================================================


# DescribeTableVersionResponse

Response containing the table version information

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**version** | [**TableVersion**](TableVersion.md) | The table version information |  |






================================================
FILE: docs/src/namespace/operations/models/DescribeTransactionRequest.md
================================================


# DescribeTransactionRequest


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** |  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/DescribeTransactionResponse.md
================================================


# DescribeTransactionResponse


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**status** | **String** | The status of a transaction. Case insensitive, supports both PascalCase and snake_case. Valid values are: - Queued: the transaction is queued and not yet started - Running: the transaction is currently running - Succeeded: the transaction has completed successfully - Failed: the transaction has failed - Canceled: the transaction was canceled  |  |
|**properties** | **Map&lt;String, String&gt;** |  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/DropNamespaceRequest.md
================================================


# DropNamespaceRequest


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** |  |  [optional] |
|**mode** | **String** | The mode for dropping a namespace, deciding the server behavior when the namespace to drop is not found. Case insensitive, supports both PascalCase and snake_case. Valid values are: - Fail (default): the server must return 400 indicating the namespace to drop does not exist. - Skip: the server must return 204 indicating the drop operation has succeeded.  |  [optional] |
|**behavior** | **String** | The behavior for dropping a namespace. Case insensitive, supports both PascalCase and snake_case. Valid values are: - Restrict (default): the namespace should not contain any table or child namespace when drop is initiated.     If tables are found, the server should return error and not drop the namespace. - Cascade: all tables and child namespaces in the namespace are dropped before the namespace is dropped.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/DropNamespaceResponse.md
================================================


# DropNamespaceResponse


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**properties** | **Map&lt;String, String&gt;** | If the implementation does not support namespace properties, it should return null for this field. Otherwise it should return the properties.  |  [optional] |
|**transactionId** | **List&lt;String&gt;** | If present, indicating the operation is long running and should be tracked using DescribeTransaction  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/DropTableIndexRequest.md
================================================


# DropTableIndexRequest


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** |  |  [optional] |
|**branch** | **String** | Branch to target. When not specified, the main branch is used.  |  [optional] |
|**indexName** | **String** | Name of the index to drop |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/DropTableIndexResponse.md
================================================


# DropTableIndexResponse

Response for drop index operation

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**transactionId** | **String** | Optional transaction identifier |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/DropTableRequest.md
================================================


# DropTableRequest

If the table and its data can be immediately deleted, return information of the deleted table. Otherwise, return a transaction ID that client can use to track deletion progress. 

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** |  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/DropTableResponse.md
================================================


# DropTableResponse


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**transactionId** | **String** | Optional transaction identifier |  [optional] |
|**id** | **List&lt;String&gt;** |  |  [optional] |
|**location** | **String** |  |  [optional] |
|**properties** | **Map&lt;String, String&gt;** | If the implementation does not support table properties, it should return null for this field. Otherwise it should return the properties.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/ErrorResponse.md
================================================


# ErrorResponse

Common JSON error response model

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**error** | **String** | A brief, human-readable message about the error. |  [optional] |
|**code** | **Integer** | Lance Namespace error code identifying the error type.  Error codes:   0 - Unsupported: Operation not supported by this backend   1 - NamespaceNotFound: The specified namespace does not exist   2 - NamespaceAlreadyExists: A namespace with this name already exists   3 - NamespaceNotEmpty: Namespace contains tables or child namespaces   4 - TableNotFound: The specified table does not exist   5 - TableAlreadyExists: A table with this name already exists   6 - TableIndexNotFound: The specified table index does not exist   7 - TableIndexAlreadyExists: A table index with this name already exists   8 - TableTagNotFound: The specified table tag does not exist   9 - TableTagAlreadyExists: A table tag with this name already exists   10 - TransactionNotFound: The specified transaction does not exist   11 - TableVersionNotFound: The specified table version does not exist   12 - TableColumnNotFound: The specified table field does not exist   13 - InvalidInput: Malformed request or invalid parameters   14 - ConcurrentModification: Optimistic concurrency conflict   15 - PermissionDenied: User lacks permission for this operation   16 - Unauthenticated: Authentication credentials are missing or invalid   17 - ServiceUnavailable: Service is temporarily unavailable   18 - Internal: Unexpected server/implementation error   19 - InvalidTableState: Table is in an invalid state for the operation   20 - TableSchemaValidationError: Table schema validation failed   21 - Throttling: Request rate limit exceeded   22 - TableBranchNotFound: The specified table branch does not exist   23 - TableBranchAlreadyExists: A table branch with this name already exists  |  |
|**detail** | **String** | An optional human-readable explanation of the error. This can be used to record additional information such as stack trace.  |  [optional] |
|**instance** | **String** | A string that identifies the specific occurrence of the error. This can be a URI, a request or response ID, or anything that the implementation can recognize to trace specific occurrence of the error.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/ExplainTableQueryPlanRequest.md
================================================


# ExplainTableQueryPlanRequest


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** |  |  [optional] |
|**branch** | **String** | Branch to target. When not specified, the main branch is used.  |  [optional] |
|**query** | [**QueryTableRequest**](QueryTableRequest.md) |  |  |
|**verbose** | **Boolean** | Whether to return verbose explanation |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/ExplainTableQueryPlanResponse.md
================================================


# ExplainTableQueryPlanResponse


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**plan** | **String** | Human-readable query execution plan |  |






================================================
FILE: docs/src/namespace/operations/models/FragmentStats.md
================================================


# FragmentStats


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**numFragments** | **Long** | The number of fragments in the table |  |
|**numSmallFragments** | **Long** | The number of uncompacted fragments in the table |  |
|**lengths** | [**FragmentSummary**](FragmentSummary.md) | Statistics on the number of rows in the table fragments |  |






================================================
FILE: docs/src/namespace/operations/models/FragmentSummary.md
================================================


# FragmentSummary


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**min** | **Long** |  |  |
|**max** | **Long** |  |  |
|**mean** | **Long** |  |  |
|**p25** | **Long** |  |  |
|**p50** | **Long** |  |  |
|**p75** | **Long** |  |  |
|**p99** | **Long** |  |  |






================================================
FILE: docs/src/namespace/operations/models/FtsQuery.md
================================================


# FtsQuery

Full-text search query. Exactly one query type field must be provided. This structure follows the same pattern as AlterTransactionAction to minimize differences and compatibility issues across codegen in different languages. 

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**match** | [**MatchQuery**](MatchQuery.md) |  |  [optional] |
|**phrase** | [**PhraseQuery**](PhraseQuery.md) |  |  [optional] |
|**boost** | [**BoostQuery**](BoostQuery.md) |  |  [optional] |
|**multiMatch** | [**MultiMatchQuery**](MultiMatchQuery.md) |  |  [optional] |
|**_boolean** | [**BooleanQuery**](BooleanQuery.md) |  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/GetTableStatsRequest.md
================================================


# GetTableStatsRequest


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** |  |  [optional] |
|**branch** | **String** | Branch to target. When not specified, the main branch is used.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/GetTableStatsResponse.md
================================================


# GetTableStatsResponse


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**totalBytes** | **Long** | The total number of bytes in the table |  |
|**numRows** | **Long** | The number of rows in the table |  |
|**numIndices** | **Long** | The number of indices in the table |  |
|**fragmentStats** | [**FragmentStats**](FragmentStats.md) | Statistics on table fragments |  |






================================================
FILE: docs/src/namespace/operations/models/GetTableTagVersionRequest.md
================================================


# GetTableTagVersionRequest


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** |  |  [optional] |
|**tag** | **String** | Name of the tag to get version for |  |






================================================
FILE: docs/src/namespace/operations/models/GetTableTagVersionResponse.md
================================================


# GetTableTagVersionResponse


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**version** | **Long** | version number that the tag points to |  |
|**branch** | **String** | Branch the tag&#39;s version lives on. Absent when the tag points to the main branch.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/Identity.md
================================================


# Identity

Identity information of a request. 

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**apiKey** | **String** | API key for authentication.  REST NAMESPACE ONLY This is passed via the &#x60;x-api-key&#x60; header.  |  [optional] |
|**authToken** | **String** | Bearer token for authentication.  REST NAMESPACE ONLY This is passed via the &#x60;Authorization&#x60; header with the Bearer scheme (e.g., &#x60;Bearer &lt;token&gt;&#x60;).  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/IndexContent.md
================================================


# IndexContent


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**indexName** | **String** | Name of the index |  |
|**indexUuid** | **String** | Unique identifier for the index |  |
|**columns** | **List&lt;String&gt;** | Canonical Lance field paths covered by this index. Nested fields use dot-separated segments; segments containing literal dots are backtick-quoted, and backticks inside quoted segments are doubled. |  |
|**status** | **String** | Current status of the index |  |
|**indexType** | **String** | Friendly index type, e.g. IVF_PQ, BTREE. Unknown if no plugin recognizes the index. |  [optional] |
|**typeUrl** | **String** | Protobuf type URL, a precise type identifier for the index. |  [optional] |
|**numIndexedRows** | **Long** | Number of live rows covered by the index. This does not count rows that are in the index but have since been deleted. |  [optional] |
|**numUnindexedRows** | **Long** | Number of rows that are not indexed. |  [optional] |
|**sizeBytes** | **Long** | Total index size in bytes across all segments. Null for indices predating file-size tracking. |  [optional] |
|**numSegments** | **Integer** | Number of index deltas/segments. |  [optional] |
|**createdAt** | **OffsetDateTime** | Creation time for indexes. Null for legacy indices. |  [optional] |
|**indexVersion** | **Integer** | On-disk index format version. |  [optional] |
|**indexDetails** | **String** | Opaque, type-specific JSON with additional index details. For vector indices this carries metric/distance type, partitioning, and HNSW/PQ/SQ/RQ parameters. |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/InsertIntoTableRequest.md
================================================


# InsertIntoTableRequest

Request for inserting records into a table, excluding the Arrow IPC stream. 

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** |  |  [optional] |
|**branch** | **String** | Branch to target. When not specified, the main branch is used.  |  [optional] |
|**mode** | **String** | How the insert should behave. Case insensitive, supports both PascalCase and snake_case. Valid values are: - Append (default): insert data to the existing table - Overwrite: remove all data in the table and then insert data to it  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/InsertIntoTableResponse.md
================================================


# InsertIntoTableResponse

Response from inserting records into a table

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**transactionId** | **String** | Optional transaction identifier |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/JsonArrowDataType.md
================================================


# JsonArrowDataType

JSON representation of an Apache Arrow DataType

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**fields** | [**List&lt;JsonArrowField&gt;**](JsonArrowField.md) | Fields for complex types like Struct, Union, etc. |  [optional] |
|**length** | **Long** | Length for fixed-size types |  [optional] |
|**type** | **String** | The data type name |  |






================================================
FILE: docs/src/namespace/operations/models/JsonArrowField.md
================================================


# JsonArrowField

JSON representation of an Apache Arrow field. 

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**metadata** | **Map&lt;String, String&gt;** |  |  [optional] |
|**name** | **String** |  |  |
|**nullable** | **Boolean** |  |  |
|**type** | [**JsonArrowDataType**](JsonArrowDataType.md) |  |  |






================================================
FILE: docs/src/namespace/operations/models/JsonArrowSchema.md
================================================


# JsonArrowSchema

JSON representation of a Apache Arrow schema. 

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**fields** | [**List&lt;JsonArrowField&gt;**](JsonArrowField.md) |  |  |
|**metadata** | **Map&lt;String, String&gt;** |  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/ListNamespacesRequest.md
================================================


# ListNamespacesRequest


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** |  |  [optional] |
|**pageToken** | **String** | An opaque token that allows pagination for list operations (e.g. ListNamespaces).  For an initial request of a list operation, if the implementation cannot return all items in one response, or if there are more items than the page limit specified in the request, the implementation must return a page token in the response, indicating there are more results available.  After the initial request, the value of the page token from each response must be used as the page token value for the next request.  Caller must interpret either &#x60;null&#x60;, missing value or empty string value of the page token from the implementation&#39;s response as the end of the listing results.  |  [optional] |
|**limit** | **Integer** | An inclusive upper bound of the number of results that a caller will receive.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/ListNamespacesResponse.md
================================================


# ListNamespacesResponse


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**namespaces** | **Set&lt;String&gt;** | The list of names of the child namespaces relative to the parent namespace &#x60;id&#x60; in the request.  |  |
|**pageToken** | **String** | An opaque token that allows pagination for list operations (e.g. ListNamespaces).  For an initial request of a list operation, if the implementation cannot return all items in one response, or if there are more items than the page limit specified in the request, the implementation must return a page token in the response, indicating there are more results available.  After the initial request, the value of the page token from each response must be used as the page token value for the next request.  Caller must interpret either &#x60;null&#x60;, missing value or empty string value of the page token from the implementation&#39;s response as the end of the listing results.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/ListTableBranchesRequest.md
================================================


# ListTableBranchesRequest


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** | The table identifier |  [optional] |
|**pageToken** | **String** | An opaque token that allows pagination for list operations (e.g. ListNamespaces).  For an initial request of a list operation, if the implementation cannot return all items in one response, or if there are more items than the page limit specified in the request, the implementation must return a page token in the response, indicating there are more results available.  After the initial request, the value of the page token from each response must be used as the page token value for the next request.  Caller must interpret either &#x60;null&#x60;, missing value or empty string value of the page token from the implementation&#39;s response as the end of the listing results.  |  [optional] |
|**limit** | **Integer** | An inclusive upper bound of the number of results that a caller will receive.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/ListTableBranchesResponse.md
================================================


# ListTableBranchesResponse

Response containing table branches

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**branches** | [**Map&lt;String, BranchContents&gt;**](BranchContents.md) | Map of branch names to their contents |  |
|**pageToken** | **String** | An opaque token that allows pagination for list operations (e.g. ListNamespaces).  For an initial request of a list operation, if the implementation cannot return all items in one response, or if there are more items than the page limit specified in the request, the implementation must return a page token in the response, indicating there are more results available.  After the initial request, the value of the page token from each response must be used as the page token value for the next request.  Caller must interpret either &#x60;null&#x60;, missing value or empty string value of the page token from the implementation&#39;s response as the end of the listing results.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/ListTableIndicesRequest.md
================================================


# ListTableIndicesRequest


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** | The namespace identifier |  [optional] |
|**version** | **Long** | Optional table version to list indexes from |  [optional] |
|**branch** | **String** | Branch to target. When not specified, the main branch is used.  |  [optional] |
|**pageToken** | **String** | An opaque token that allows pagination for list operations (e.g. ListNamespaces).  For an initial request of a list operation, if the implementation cannot return all items in one response, or if there are more items than the page limit specified in the request, the implementation must return a page token in the response, indicating there are more results available.  After the initial request, the value of the page token from each response must be used as the page token value for the next request.  Caller must interpret either &#x60;null&#x60;, missing value or empty string value of the page token from the implementation&#39;s response as the end of the listing results.  |  [optional] |
|**limit** | **Integer** | An inclusive upper bound of the number of results that a caller will receive.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/ListTableIndicesResponse.md
================================================


# ListTableIndicesResponse


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**indexes** | [**List&lt;IndexContent&gt;**](IndexContent.md) | List of indexes on the table |  |
|**pageToken** | **String** | An opaque token that allows pagination for list operations (e.g. ListNamespaces).  For an initial request of a list operation, if the implementation cannot return all items in one response, or if there are more items than the page limit specified in the request, the implementation must return a page token in the response, indicating there are more results available.  After the initial request, the value of the page token from each response must be used as the page token value for the next request.  Caller must interpret either &#x60;null&#x60;, missing value or empty string value of the page token from the implementation&#39;s response as the end of the listing results.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/ListTablesRequest.md
================================================


# ListTablesRequest


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** |  |  [optional] |
|**pageToken** | **String** | An opaque token that allows pagination for list operations (e.g. ListNamespaces).  For an initial request of a list operation, if the implementation cannot return all items in one response, or if there are more items than the page limit specified in the request, the implementation must return a page token in the response, indicating there are more results available.  After the initial request, the value of the page token from each response must be used as the page token value for the next request.  Caller must interpret either &#x60;null&#x60;, missing value or empty string value of the page token from the implementation&#39;s response as the end of the listing results.  |  [optional] |
|**limit** | **Integer** | An inclusive upper bound of the number of results that a caller will receive.  |  [optional] |
|**includeDeclared** | **Boolean** | When true (default), includes tables that have been declared in the namespace but not yet created on storage, in addition to tables that have been created. When false, only tables with storage components are returned.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/ListTablesResponse.md
================================================


# ListTablesResponse


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**tables** | **Set&lt;String&gt;** | The list of names of all the tables under the connected namespace implementation. This should recursively list all the tables in all child namespaces. Each string in the list is the full identifier in string form.  |  |
|**pageToken** | **String** | An opaque token that allows pagination for list operations (e.g. ListNamespaces).  For an initial request of a list operation, if the implementation cannot return all items in one response, or if there are more items than the page limit specified in the request, the implementation must return a page token in the response, indicating there are more results available.  After the initial request, the value of the page token from each response must be used as the page token value for the next request.  Caller must interpret either &#x60;null&#x60;, missing value or empty string value of the page token from the implementation&#39;s response as the end of the listing results.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/ListTableTagsRequest.md
================================================


# ListTableTagsRequest


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** | The table identifier |  [optional] |
|**pageToken** | **String** | An opaque token that allows pagination for list operations (e.g. ListNamespaces).  For an initial request of a list operation, if the implementation cannot return all items in one response, or if there are more items than the page limit specified in the request, the implementation must return a page token in the response, indicating there are more results available.  After the initial request, the value of the page token from each response must be used as the page token value for the next request.  Caller must interpret either &#x60;null&#x60;, missing value or empty string value of the page token from the implementation&#39;s response as the end of the listing results.  |  [optional] |
|**limit** | **Integer** | An inclusive upper bound of the number of results that a caller will receive.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/ListTableTagsResponse.md
================================================


# ListTableTagsResponse

Response containing table tags

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**tags** | [**Map&lt;String, TagContents&gt;**](TagContents.md) | Map of tag names to their contents |  |
|**pageToken** | **String** | An opaque token that allows pagination for list operations (e.g. ListNamespaces).  For an initial request of a list operation, if the implementation cannot return all items in one response, or if there are more items than the page limit specified in the request, the implementation must return a page token in the response, indicating there are more results available.  After the initial request, the value of the page token from each response must be used as the page token value for the next request.  Caller must interpret either &#x60;null&#x60;, missing value or empty string value of the page token from the implementation&#39;s response as the end of the listing results.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/ListTableVersionsRequest.md
================================================


# ListTableVersionsRequest


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** |  |  [optional] |
|**branch** | **String** | Branch to target. When not specified, the main branch is used.  |  [optional] |
|**pageToken** | **String** | An opaque token that allows pagination for list operations (e.g. ListNamespaces).  For an initial request of a list operation, if the implementation cannot return all items in one response, or if there are more items than the page limit specified in the request, the implementation must return a page token in the response, indicating there are more results available.  After the initial request, the value of the page token from each response must be used as the page token value for the next request.  Caller must interpret either &#x60;null&#x60;, missing value or empty string value of the page token from the implementation&#39;s response as the end of the listing results.  |  [optional] |
|**limit** | **Integer** | An inclusive upper bound of the number of results that a caller will receive.  |  [optional] |
|**descending** | **Boolean** | When true, versions are guaranteed to be returned in descending order (latest to oldest). When false or not specified, the ordering is implementation-defined.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/ListTableVersionsResponse.md
================================================


# ListTableVersionsResponse


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**versions** | [**List&lt;TableVersion&gt;**](TableVersion.md) | List of table versions. When &#x60;descending&#x3D;true&#x60;, guaranteed to be ordered from latest to oldest. Otherwise, ordering is implementation-defined.  |  |
|**pageToken** | **String** | An opaque token that allows pagination for list operations (e.g. ListNamespaces).  For an initial request of a list operation, if the implementation cannot return all items in one response, or if there are more items than the page limit specified in the request, the implementation must return a page token in the response, indicating there are more results available.  After the initial request, the value of the page token from each response must be used as the page token value for the next request.  Caller must interpret either &#x60;null&#x60;, missing value or empty string value of the page token from the implementation&#39;s response as the end of the listing results.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/MatchQuery.md
================================================


# MatchQuery


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**boost** | **Float** |  |  [optional] |
|**column** | **String** | Lance field path to match. Nested fields use dot-separated segments; use backtick-quoted segments for literal dots and double backticks inside quoted segments. Omit to use the query default fields. |  [optional] |
|**fuzziness** | **Integer** |  |  [optional] |
|**maxExpansions** | **Integer** | The maximum number of terms to expand for fuzzy matching. Default to 50. |  [optional] |
|**operator** | **String** | The operator to use for combining terms. Case insensitive, supports both PascalCase and snake_case. Valid values are: - And: All terms must match. - Or: At least one term must match.  |  [optional] |
|**prefixLength** | **Integer** | The number of beginning characters being unchanged for fuzzy matching. Default to 0. |  [optional] |
|**terms** | **String** |  |  |






================================================
FILE: docs/src/namespace/operations/models/MaterializedViewApi.md
================================================
# MaterializedViewApi

All URIs are relative to *http://localhost:2333*

| Method | HTTP request | Description |
|------------- | ------------- | -------------|
| [**createMaterializedView**](MaterializedViewApi.md#createMaterializedView) | **POST** /v1/materialized_view/{id}/create | Create a materialized view |
| [**refreshMaterializedView**](MaterializedViewApi.md#refreshMaterializedView) | **POST** /v1/materialized_view/{id}/refresh | Trigger an async materialized view refresh |



## createMaterializedView

> CreateMaterializedViewResponse createMaterializedView(id, createMaterializedViewRequest, delimiter)

Create a materialized view

Create a materialized view at identifier &#x60;id&#x60;. The view may be query-backed, UDTF-backed, or chunker-backed, controlled by the &#x60;kind&#x60; discriminator. 

### Example

```java
// Import classes:
import org.lance.namespace.client.apache.ApiClient;
import org.lance.namespace.client.apache.ApiException;
import org.lance.namespace.client.apache.Configuration;
import org.lance.namespace.client.apache.auth.*;
import org.lance.namespace.client.apache.models.*;
import org.lance.namespace.client.apache.api.MaterializedViewApi;

public class Example {
    public static void main(String[] args) {
        ApiClient defaultClient = Configuration.getDefaultApiClient();
        defaultClient.setBasePath("http://localhost:2333");
        
        // Configure OAuth2 access token for authorization: OAuth2
        OAuth OAuth2 = (OAuth) defaultClient.getAuthentication("OAuth2");
        OAuth2.setAccessToken("YOUR ACCESS TOKEN");

        // Configure API key authorization: ApiKeyAuth
        ApiKeyAuth ApiKeyAuth = (ApiKeyAuth) defaultClient.getAuthentication("ApiKeyAuth");
        ApiKeyAuth.setApiKey("YOUR API KEY");
        // Uncomment the following line to set a prefix for the API key, e.g. "Token" (defaults to null)
        //ApiKeyAuth.setApiKeyPrefix("Token");

        // Configure HTTP bearer authorization: BearerAuth
        HttpBearerAuth BearerAuth = (HttpBearerAuth) defaultClient.getAuthentication("BearerAuth");
        BearerAuth.setBearerToken("BEARER TOKEN");

        MaterializedViewApi apiInstance = new MaterializedViewApi(defaultClient);
        String id = "id_example"; // String | `string identifier` of an object in a namespace, following the Lance Namespace spec. When the value is equal to the delimiter, it represents the root namespace. For example, `v1/namespace/$/list` performs a `ListNamespace` on the root namespace. 
        CreateMaterializedViewRequest createMaterializedViewRequest = new CreateMaterializedViewRequest(); // CreateMaterializedViewRequest | 
        String delimiter = "delimiter_example"; // String | An optional delimiter of the `string identifier`, following the Lance Namespace spec. When not specified, the `$` delimiter must be used. 
        try {
            CreateMaterializedViewResponse result = apiInstance.createMaterializedView(id, createMaterializedViewRequest, delimiter);
            System.out.println(result);
        } catch (ApiException e) {
            System.err.println("Exception when calling MaterializedViewApi#createMaterializedView");
            System.err.println("Status code: " + e.getCode());
            System.err.println("Reason: " + e.getResponseBody());
            System.err.println("Response headers: " + e.getResponseHeaders());
            e.printStackTrace();
        }
    }
}
```

### Parameters


| Name | Type | Description  | Notes |
|------------- | ------------- | ------------- | -------------|
| **id** | **String**| &#x60;string identifier&#x60; of an object in a namespace, following the Lance Namespace spec. When the value is equal to the delimiter, it represents the root namespace. For example, &#x60;v1/namespace/$/list&#x60; performs a &#x60;ListNamespace&#x60; on the root namespace.  | |
| **createMaterializedViewRequest** | [**CreateMaterializedViewRequest**](CreateMaterializedViewRequest.md)|  | |
| **delimiter** | **String**| An optional delimiter of the &#x60;string identifier&#x60;, following the Lance Namespace spec. When not specified, the &#x60;$&#x60; delimiter must be used.  | [optional] |

### Return type

[**CreateMaterializedViewResponse**](CreateMaterializedViewResponse.md)

### Authorization

[OAuth2](../README.md#OAuth2), [ApiKeyAuth](../README.md#ApiKeyAuth), [BearerAuth](../README.md#BearerAuth)

### HTTP request headers

- **Content-Type**: application/json
- **Accept**: application/json


### HTTP response details
| Status code | Description | Response headers |
|-------------|-------------|------------------|
| **201** | Materialized view created |  -  |
| **400** | Indicates a bad request error. It could be caused by an unexpected request body format or other forms of request validation failure, such as invalid json. Usually serves application/json content, although in some cases simple text/plain content might be returned by the server&#39;s middleware. |  -  |
| **401** | Unauthorized. The request lacks valid authentication credentials for the operation. |  -  |
| **403** | Forbidden. Authenticated user does not have the necessary permissions. |  -  |
| **404** | A server-side problem that means can not find the specified resource. |  -  |
| **409** | The request conflicts with the current state of the target resource. |  -  |
| **503** | The service is not ready to handle the request. The client should wait and retry. The service may additionally send a Retry-After header to indicate when to retry. |  -  |
| **5XX** | A server-side problem that might not be addressable from the client side. Used for server 5xx errors without more specific documentation in individual routes. |  -  |


## refreshMaterializedView

> RefreshMaterializedViewResponse refreshMaterializedView(id, delimiter, refreshMaterializedViewRequest)

Trigger an async materialized view refresh

Trigger an asynchronous refresh job for materialized view &#x60;id&#x60;. Returns a job ID for tracking. 

### Example

```java
// Import classes:
import org.lance.namespace.client.apache.ApiClient;
import org.lance.namespace.client.apache.ApiException;
import org.lance.namespace.client.apache.Configuration;
import org.lance.namespace.client.apache.auth.*;
import org.lance.namespace.client.apache.models.*;
import org.lance.namespace.client.apache.api.MaterializedViewApi;

public class Example {
    public static void main(String[] args) {
        ApiClient defaultClient = Configuration.getDefaultApiClient();
        defaultClient.setBasePath("http://localhost:2333");
        
        // Configure OAuth2 access token for authorization: OAuth2
        OAuth OAuth2 = (OAuth) defaultClient.getAuthentication("OAuth2");
        OAuth2.setAccessToken("YOUR ACCESS TOKEN");

        // Configure API key authorization: ApiKeyAuth
        ApiKeyAuth ApiKeyAuth = (ApiKeyAuth) defaultClient.getAuthentication("ApiKeyAuth");
        ApiKeyAuth.setApiKey("YOUR API KEY");
        // Uncomment the following line to set a prefix for the API key, e.g. "Token" (defaults to null)
        //ApiKeyAuth.setApiKeyPrefix("Token");

        // Configure HTTP bearer authorization: BearerAuth
        HttpBearerAuth BearerAuth = (HttpBearerAuth) defaultClient.getAuthentication("BearerAuth");
        BearerAuth.setBearerToken("BEARER TOKEN");

        MaterializedViewApi apiInstance = new MaterializedViewApi(defaultClient);
        String id = "id_example"; // String | `string identifier` of an object in a namespace, following the Lance Namespace spec. When the value is equal to the delimiter, it represents the root namespace. For example, `v1/namespace/$/list` performs a `ListNamespace` on the root namespace. 
        String delimiter = "delimiter_example"; // String | An optional delimiter of the `string identifier`, following the Lance Namespace spec. When not specified, the `$` delimiter must be used. 
        RefreshMaterializedViewRequest refreshMaterializedViewRequest = new RefreshMaterializedViewRequest(); // RefreshMaterializedViewRequest | 
        try {
            RefreshMaterializedViewResponse result = apiInstance.refreshMaterializedView(id, delimiter, refreshMaterializedViewRequest);
            System.out.println(result);
        } catch (ApiException e) {
            System.err.println("Exception when calling MaterializedViewApi#refreshMaterializedView");
            System.err.println("Status code: " + e.getCode());
            System.err.println("Reason: " + e.getResponseBody());
            System.err.println("Response headers: " + e.getResponseHeaders());
            e.printStackTrace();
        }
    }
}
```

### Parameters


| Name | Type | Description  | Notes |
|------------- | ------------- | ------------- | -------------|
| **id** | **String**| &#x60;string identifier&#x60; of an object in a namespace, following the Lance Namespace spec. When the value is equal to the delimiter, it represents the root namespace. For example, &#x60;v1/namespace/$/list&#x60; performs a &#x60;ListNamespace&#x60; on the root namespace.  | |
| **delimiter** | **String**| An optional delimiter of the &#x60;string identifier&#x60;, following the Lance Namespace spec. When not specified, the &#x60;$&#x60; delimiter must be used.  | [optional] |
| **refreshMaterializedViewRequest** | [**RefreshMaterializedViewRequest**](RefreshMaterializedViewRequest.md)|  | [optional] |

### Return type

[**RefreshMaterializedViewResponse**](RefreshMaterializedViewResponse.md)

### Authorization

[OAuth2](../README.md#OAuth2), [ApiKeyAuth](../README.md#ApiKeyAuth), [BearerAuth](../README.md#BearerAuth)

### HTTP request headers

- **Content-Type**: application/json
- **Accept**: application/json


### HTTP response details
| Status code | Description | Response headers |
|-------------|-------------|------------------|
| **202** | Refresh job accepted |  -  |
| **400** | Indicates a bad request error. It could be caused by an unexpected request body format or other forms of request validation failure, such as invalid json. Usually serves application/json content, although in some cases simple text/plain content might be returned by the server&#39;s middleware. |  -  |
| **401** | Unauthorized. The request lacks valid authentication credentials for the operation. |  -  |
| **403** | Forbidden. Authenticated user does not have the necessary permissions. |  -  |
| **404** | A server-side problem that means can not find the specified resource. |  -  |
| **503** | The service is not ready to handle the request. The client should wait and retry. The service may additionally send a Retry-After header to indicate when to retry. |  -  |
| **5XX** | A server-side problem that might not be addressable from the client side. Used for server 5xx errors without more specific documentation in individual routes. |  -  |




================================================
FILE: docs/src/namespace/operations/models/MaterializedViewUdtfEntry.md
================================================


# MaterializedViewUdtfEntry


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**kind** | [**KindEnum**](#KindEnum) | Discriminates a batch UDTF (&#x60;udtf&#x60;, full-overwrite refresh) from a chunker (&#x60;chunker&#x60;, incremental 1:N refresh). Must match the enclosing request&#39;s &#x60;kind&#x60;.  |  |
|**udtf** | **String** | Base64-encoded UDTFSpec / ChunkerSpec JSON envelope (per kind).  |  |
|**udtfSha** | **String** | SHA-256 checksum of the envelope; server validates. |  |
|**udtfName** | **String** | Name of the UDTF |  |
|**udtfVersion** | **String** | Version of the UDTF |  |
|**inputColumns** | **List&lt;String&gt;** | Source Lance field paths the UDTF reads. Nested fields use dot-separated segments; use backtick-quoted segments for literal dots and double backticks inside quoted segments. Null means all fields (batch UDTF only).  |  [optional] |
|**partitionBy** | **String** |  |  [optional] |
|**partitionByIndexedColumn** | **String** |  |  [optional] |
|**numCpus** | **BigDecimal** | Ray actor CPU request. |  [optional] |
|**numGpus** | **BigDecimal** | Ray actor GPU request. |  [optional] |
|**memory** | **Integer** | Ray actor memory request, in bytes. |  [optional] |
|**errorHandling** | **Object** | Batch UDTF only. Serialized ErrorHandlingConfig controlling partition-grain fail/retry/skip behavior.  |  [optional] |
|**batch** | **Boolean** | Chunker only. True for a batched chunker; affects how the worker dispatches input rows.  |  [optional] |
|**manifest** | **String** | JSON-serialized GenevaManifest for the UDTF environment. |  [optional] |
|**manifestChecksum** | **String** | SHA-256 checksum of the manifest content. |  [optional] |



## Enum: KindEnum

| Name | Value |
|---- | -----|
| UDTF | &quot;udtf&quot; |
| CHUNKER | &quot;chunker&quot; |






================================================
FILE: docs/src/namespace/operations/models/MergeInsertIntoTableRequest.md
================================================


# MergeInsertIntoTableRequest

Request for merging or inserting records into a table, excluding the Arrow IPC stream. 

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** |  |  [optional] |
|**branch** | **String** | Branch to target. When not specified, the main branch is used.  |  [optional] |
|**on** | **String** | Lance field path to use for matching rows. Nested fields use dot-separated segments; use backtick-quoted segments for literal dots and double backticks inside quoted segments. Use canonical full paths for display and errors; leaf names alone only identify top-level fields; invalid or unresolved paths should return InvalidInput or TableColumnNotFound. |  [optional] |
|**whenMatchedUpdateAll** | **Boolean** | Update all columns when rows match |  [optional] |
|**whenMatchedUpdateAllFilt** | **String** | The row is updated (similar to UpdateAll) only for rows where the SQL expression evaluates to true. Field references must use Lance field path syntax: nested fields use dot-separated segments, literal dots require backtick-quoted segments, and backticks inside quoted segments are doubled. |  [optional] |
|**whenNotMatchedInsertAll** | **Boolean** | Insert all columns when rows don&#39;t match |  [optional] |
|**whenNotMatchedBySourceDelete** | **Boolean** | Delete all rows from target table that don&#39;t match a row in the source table |  [optional] |
|**whenNotMatchedBySourceDeleteFilt** | **String** | Delete rows from the target table if there is no match AND the SQL expression evaluates to true. Field references must use Lance field path syntax: nested fields use dot-separated segments, literal dots require backtick-quoted segments, and backticks inside quoted segments are doubled. |  [optional] |
|**timeout** | **String** | Timeout for the operation (e.g., \&quot;30s\&quot;, \&quot;5m\&quot;) |  [optional] |
|**useIndex** | **Boolean** | Whether to use index for matching rows |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/MergeInsertIntoTableResponse.md
================================================


# MergeInsertIntoTableResponse

Response from merge insert operation

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**transactionId** | **String** | Optional transaction identifier |  [optional] |
|**numUpdatedRows** | **Long** | Number of rows updated |  [optional] |
|**numInsertedRows** | **Long** | Number of rows inserted |  [optional] |
|**numDeletedRows** | **Long** | Number of rows deleted (typically 0 for merge insert) |  [optional] |
|**version** | **Long** | The commit version associated with the operation |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/MultiMatchQuery.md
================================================


# MultiMatchQuery


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**matchQueries** | [**List&lt;MatchQuery&gt;**](MatchQuery.md) |  |  |






================================================
FILE: docs/src/namespace/operations/models/NamespaceExistsRequest.md
================================================


# NamespaceExistsRequest


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** |  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/NamespaceExistsResponse.md
================================================


# NamespaceExistsResponse

Response for a namespace existence check.  The REST namespace does not transmit this object directly (see the NamespaceExists operation for how the status-code response maps to it). It is the standard data model for the LanceNamespace interfaces (e.g. Java, Python). 

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/PartitionField.md
================================================


# PartitionField

Partition field definition

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**fieldId** | **String** | Unique identifier for this partition field (must not be renamed) |  |
|**sourceIds** | **List&lt;Integer&gt;** | Field IDs of the source fields in the schema |  |
|**transform** | [**PartitionTransform**](PartitionTransform.md) | Well-known partition transform. Exactly one of transform or expression must be specified. |  [optional] |
|**expression** | **String** | DataFusion SQL expression using col0, col1, ... as column references. Exactly one of transform or expression must be specified. |  [optional] |
|**resultType** | [**JsonArrowDataType**](JsonArrowDataType.md) | The output type of the partition value (JsonArrowDataType format) |  |






================================================
FILE: docs/src/namespace/operations/models/PartitionSpec.md
================================================


# PartitionSpec

Partition spec definition

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**id** | **Integer** | The spec version ID |  |
|**fields** | [**List&lt;PartitionField&gt;**](PartitionField.md) | Array of partition field definitions |  |






================================================
FILE: docs/src/namespace/operations/models/PartitionTransform.md
================================================


# PartitionTransform

Well-known partition transform

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**type** | **String** | Transform type (identity, year, month, day, hour, bucket, multi_bucket, truncate) |  |
|**numBuckets** | **Integer** | Number of buckets for bucket transforms |  [optional] |
|**width** | **Integer** | Truncation width for truncate transforms |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/PhraseQuery.md
================================================


# PhraseQuery


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**column** | **String** | Lance field path to match. Nested fields use dot-separated segments; use backtick-quoted segments for literal dots and double backticks inside quoted segments. Omit to use the query default fields. |  [optional] |
|**slop** | **Integer** |  |  [optional] |
|**terms** | **String** |  |  |






================================================
FILE: docs/src/namespace/operations/models/QueryTableRequest.md
================================================


# QueryTableRequest


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** |  |  [optional] |
|**branch** | **String** | Branch to target. When not specified, the main branch is used.  |  [optional] |
|**bypassVectorIndex** | **Boolean** | Whether to bypass vector index |  [optional] |
|**columns** | [**QueryTableRequestColumns**](QueryTableRequestColumns.md) |  |  [optional] |
|**distanceType** | **String** | Distance metric to use |  [optional] |
|**ef** | **Integer** | Search effort parameter for HNSW index |  [optional] |
|**fastSearch** | **Boolean** | Whether to use fast search |  [optional] |
|**filter** | **String** | Optional SQL filter expression. Field references in the expression must use Lance field path syntax: nested fields use dot-separated segments, literal dots require backtick-quoted segments, and backticks inside quoted segments are doubled.  |  [optional] |
|**fullTextQuery** | [**QueryTableRequestFullTextQuery**](QueryTableRequestFullTextQuery.md) |  |  [optional] |
|**k** | **Integer** | Number of results to return |  |
|**lowerBound** | **Float** | Lower bound for search |  [optional] |
|**nprobes** | **Integer** | Number of probes for IVF index |  [optional] |
|**offset** | **Integer** | Number of results to skip |  [optional] |
|**prefilter** | **Boolean** | Whether to apply filtering before vector search |  [optional] |
|**refineFactor** | **Integer** | Refine factor for search |  [optional] |
|**upperBound** | **Float** | Upper bound for search |  [optional] |
|**vector** | [**QueryTableRequestVector**](QueryTableRequestVector.md) |  |  |
|**vectorColumn** | **String** | Lance field path of the vector field to search. Nested fields use dot-separated segments; use backtick-quoted segments for literal dots and double backticks inside quoted segments. Use canonical full paths for display and errors; leaf names alone only identify top-level fields; invalid or unresolved paths should return InvalidInput or TableColumnNotFound. |  [optional] |
|**version** | **Long** | Table version to query |  [optional] |
|**withRowId** | **Boolean** | If true, return the row id as a column called &#x60;_rowid&#x60; |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/QueryTableRequestColumns.md
================================================


# QueryTableRequestColumns

Optional field paths to return. Provide either column_names or column_aliases, not both. 

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**columnNames** | **List&lt;String&gt;** | List of Lance field paths to return. Nested fields use dot-separated segments; use backtick-quoted segments for literal dots and double backticks inside quoted segments. |  [optional] |
|**columnAliases** | **Map&lt;String, String&gt;** | Object mapping output aliases to source Lance field paths. Nested fields use dot-separated segments; use backtick-quoted segments for literal dots and double backticks inside quoted segments. |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/QueryTableRequestFullTextQuery.md
================================================


# QueryTableRequestFullTextQuery

Optional full-text search query. Provide either string_query or structured_query, not both.

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**stringQuery** | [**StringFtsQuery**](StringFtsQuery.md) |  |  [optional] |
|**structuredQuery** | [**StructuredFtsQuery**](StructuredFtsQuery.md) |  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/QueryTableRequestVector.md
================================================


# QueryTableRequestVector

Query vector(s) for similarity search. Provide either single_vector or multi_vector, not both.

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**singleVector** | **List&lt;Float&gt;** | Single query vector |  [optional] |
|**multiVector** | **List&lt;List&lt;Float&gt;&gt;** | Multiple query vectors for batch search |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/QueryTableResponse.md
================================================


# QueryTableResponse

Query results.  The REST namespace does not transmit this object directly (see the QueryTable operation for how the Arrow IPC binary response maps to it). It is the standard data model for the LanceNamespace interfaces (e.g. Java, Python). 

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**data** | **byte[]** | Query results as Arrow IPC file binary data. |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/RefreshMaterializedViewRequest.md
================================================


# RefreshMaterializedViewRequest


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** | Table identifier path (namespace + table name) |  [optional] |
|**srcVersion** | **Integer** | Optional source version to refresh from |  [optional] |
|**maxRowsPerFragment** | **Integer** | Optional maximum rows per fragment |  [optional] |
|**concurrency** | **Integer** | Optional concurrency override |  [optional] |
|**intraApplierConcurrency** | **Integer** | Optional intra-applier concurrency override |  [optional] |
|**sourceTaskSize** | **Integer** | Optional number of source row ids per work item during expansion. Bounds per-actor memory for chunker materialized views.  |  [optional] |
|**cluster** | **String** | Optional cluster name (operational override) |  [optional] |
|**outputLimit** | **Integer** | Post-trim cap on view row count after expansion. Valid only for chunker materialized views; returns 400 if set on other kinds.  |  [optional] |
|**manifest** | **String** | Optional inline JSON-serialized GenevaManifest. Operational override for this refresh only; does not mutate the view&#39;s snapshotted manifest. When omitted, the manifest stored in the view&#39;s metadata is used.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/RefreshMaterializedViewResponse.md
================================================


# RefreshMaterializedViewResponse


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**jobId** | **String** | The job ID for tracking the refresh job |  |






================================================
FILE: docs/src/namespace/operations/models/RegisterTableRequest.md
================================================


# RegisterTableRequest


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** |  |  [optional] |
|**location** | **String** |  |  |
|**mode** | **String** | There are two modes when trying to register a table, to differentiate the behavior when a table of the same name already exists. Case insensitive, supports both PascalCase and snake_case. Valid values are:   * Create (default): the operation fails with 409.   * Overwrite: the existing table registration is replaced with the new registration.  |  [optional] |
|**properties** | **Map&lt;String, String&gt;** | Properties stored on the table, if supported by the implementation.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/RegisterTableResponse.md
================================================


# RegisterTableResponse


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**transactionId** | **String** | Optional transaction identifier |  [optional] |
|**location** | **String** |  |  [optional] |
|**properties** | **Map&lt;String, String&gt;** | If the implementation does not support table properties, it should return null for this field. Otherwise, it should return the properties.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/RenameTableRequest.md
================================================


# RenameTableRequest


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** | The table identifier |  [optional] |
|**newTableName** | **String** | New name for the table |  |
|**newNamespaceId** | **List&lt;String&gt;** | New namespace identifier to move the table to (optional, if not specified the table stays in the same namespace) |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/RenameTableResponse.md
================================================


# RenameTableResponse


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**transactionId** | **String** | Optional transaction identifier |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/RestoreTableRequest.md
================================================


# RestoreTableRequest


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** |  |  [optional] |
|**version** | **Long** | Version to restore to |  |
|**branch** | **String** | Branch to target. When not specified, the main branch is used.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/RestoreTableResponse.md
================================================


# RestoreTableResponse

Response for restore table operation

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**transactionId** | **String** | Optional transaction identifier |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/StringFtsQuery.md
================================================


# StringFtsQuery


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**columns** | **List&lt;String&gt;** | Lance field paths to search. Nested fields use dot-separated segments; use backtick-quoted segments for literal dots and double backticks inside quoted segments. Omit to search all indexed FTS fields. |  [optional] |
|**query** | **String** |  |  |






================================================
FILE: docs/src/namespace/operations/models/StructuredFtsQuery.md
================================================


# StructuredFtsQuery


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**query** | [**FtsQuery**](FtsQuery.md) |  |  |






================================================
FILE: docs/src/namespace/operations/models/TableBasicStats.md
================================================


# TableBasicStats


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**numDeletedRows** | **Integer** | Number of deleted rows in the table |  |
|**numFragments** | **Integer** | Number of fragments in the table |  |






================================================
FILE: docs/src/namespace/operations/models/TableExistsRequest.md
================================================


# TableExistsRequest


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** |  |  [optional] |
|**version** | **Long** | Version of the table to check existence. If not specified, server should resolve it to the latest version.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/TableExistsResponse.md
================================================


# TableExistsResponse

Response for a table existence check.  The REST namespace does not transmit this object directly (see the TableExists operation for how the status-code response maps to it). It is the standard data model for the LanceNamespace interfaces (e.g. Java, Python). 

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/TableVersion.md
================================================


# TableVersion


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**version** | **Long** | Version number |  |
|**manifestPath** | **String** | Path to the manifest file for this version. |  |
|**manifestSize** | **Long** | Size of the manifest file in bytes |  [optional] |
|**eTag** | **String** | Optional ETag for optimistic concurrency control. Useful for S3 and similar object stores.  |  [optional] |
|**timestampMillis** | **Long** | Timestamp when the version was created, in milliseconds since epoch (Unix time) |  [optional] |
|**metadata** | **Map&lt;String, String&gt;** | Optional key-value pairs of metadata |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/TagContents.md
================================================


# TagContents


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**branch** | **String** | Branch name that the tag was created on (if any) |  [optional] |
|**version** | **Long** | Version number that the tag points to |  |
|**manifestSize** | **Long** | Size of the manifest file in bytes |  |






================================================
FILE: docs/src/namespace/operations/models/UpdateFieldMetadataEntry.md
================================================


# UpdateFieldMetadataEntry


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**path** | **String** | Lance field path whose metadata to update. Nested fields use dot-separated segments; use backtick-quoted segments for literal dots and double backticks inside quoted segments. Use canonical full paths for display and errors; leaf names alone only identify top-level fields; invalid or unresolved paths should return InvalidInput or TableColumnNotFound. |  |
|**metadata** | **Map&lt;String, String&gt;** | Metadata key-value pairs to apply to the field. A null value deletes that key.  |  |
|**replace** | **Boolean** | If true, replace the field&#39;s existing metadata entirely; otherwise merge into it (optional, defaults to false).  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/UpdateFieldMetadataRequest.md
================================================


# UpdateFieldMetadataRequest


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** | Table identifier path (namespace + table name) |  [optional] |
|**branch** | **String** | Branch to target. When not specified, the main branch is used.  |  [optional] |
|**updates** | [**List&lt;UpdateFieldMetadataEntry&gt;**](UpdateFieldMetadataEntry.md) | List of per-field metadata updates to apply |  |






================================================
FILE: docs/src/namespace/operations/models/UpdateFieldMetadataResponse.md
================================================


# UpdateFieldMetadataResponse


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**version** | **Long** | The commit version associated with the operation |  |
|**fields** | **Map&lt;String, Map&lt;String, String&gt;&gt;** | Resulting metadata for each updated field, keyed by canonical Lance field path.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/UpdateTableRequest.md
================================================


# UpdateTableRequest

Each update consists of a field path and an SQL expression that will be evaluated against the current row's value. Optionally, a predicate can be provided to filter which rows to update. 

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** |  |  [optional] |
|**branch** | **String** | Branch to target. When not specified, the main branch is used.  |  [optional] |
|**predicate** | **String** | Optional SQL predicate to filter rows for update. Field references must use Lance field path syntax: nested fields use dot-separated segments, literal dots require backtick-quoted segments, and backticks inside quoted segments are doubled. |  [optional] |
|**updates** | **List&lt;List&lt;String&gt;&gt;** | List of field updates as [field_path, expression] pairs. Field paths and expression references must use Lance field path syntax: nested fields use dot-separated segments, literal dots require backtick-quoted segments, and backticks inside quoted segments are doubled. |  |
|**properties** | **Map&lt;String, String&gt;** | Properties stored on the table, if supported by the implementation.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/UpdateTableResponse.md
================================================


# UpdateTableResponse


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**transactionId** | **String** | Optional transaction identifier |  [optional] |
|**updatedRows** | **Long** | Number of rows updated |  |
|**version** | **Long** | The commit version associated with the operation |  |
|**properties** | **Map&lt;String, String&gt;** | If the implementation does not support table properties, it should return null for this field. Otherwise, it should return the properties.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/UpdateTableSchemaMetadataRequest.md
================================================


# UpdateTableSchemaMetadataRequest


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** | The table identifier |  [optional] |
|**branch** | **String** | Branch to target. When not specified, the main branch is used.  |  [optional] |
|**metadata** | **Map&lt;String, String&gt;** | Schema metadata key-value pairs to set |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/UpdateTableSchemaMetadataResponse.md
================================================


# UpdateTableSchemaMetadataResponse


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**metadata** | **Map&lt;String, String&gt;** | The updated schema metadata |  [optional] |
|**transactionId** | **String** | Optional transaction identifier |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/UpdateTableTagRequest.md
================================================


# UpdateTableTagRequest


## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**identity** | [**Identity**](Identity.md) |  |  [optional] |
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**id** | **List&lt;String&gt;** |  |  [optional] |
|**tag** | **String** | Name of the tag to update |  |
|**version** | **Long** | New version number for the tag to point to |  |
|**branch** | **String** | Branch to target. When not specified, the main branch is used.  |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/UpdateTableTagResponse.md
================================================


# UpdateTableTagResponse

Response for update tag operation

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**context** | **Map&lt;String, String&gt;** | Arbitrary context as key-value pairs. How to use the context is custom to the specific implementation.  On a request, it carries caller-provided context to the implementation. On a response, it carries implementation-provided context back to the caller.  REST NAMESPACE ONLY Context entries are mapped to and from HTTP headers using the &#x60;header.&#x60; prefix: - On a request, any entry whose key starts with &#x60;header.&#x60; is sent as an HTTP   request header with the prefix stripped. For example, the entry   &#x60;{\&quot;header.Authorization\&quot;: \&quot;Bearer abc\&quot;}&#x60; is sent as the request header   &#x60;Authorization: Bearer abc&#x60;. - On a response, every HTTP response header is returned as an entry whose key is the   header name prefixed with &#x60;header.&#x60;. For example, the response header   &#x60;x-request-id: abc123&#x60; is returned as the entry &#x60;{\&quot;header.x-request-id\&quot;: \&quot;abc123\&quot;}&#x60;.  |  [optional] |
|**transactionId** | **String** | Optional transaction identifier |  [optional] |






================================================
FILE: docs/src/namespace/operations/models/VersionRange.md
================================================


# VersionRange

A range of versions to delete (start inclusive, end exclusive). Special values: - `start_version: 0` with `end_version: -1` means ALL versions 

## Properties

| Name | Type | Description | Notes |
|------------ | ------------- | ------------- | -------------|
|**startVersion** | **Long** | Start version of the range (inclusive). Use 0 to start from the first version.  |  |
|**endVersion** | **Long** | End version of the range (exclusive). Use -1 to indicate all versions up to and including the latest.  |  |






================================================
FILE: docs/src/namespace/operations/models/.pages
================================================
title: Models



================================================
FILE: docs/src/namespace/supported-catalogs/index.md
================================================
# Supported Catalogs

Beyond the natively maintained [Directory Catalog](../../catalog/dir/index.md) and [REST Catalog](../../catalog/rest/index.md) specifications, Lance supports integration with external catalog systems through the [Namespace Client Spec](../index.md).

## What are Supported Catalogs?

Supported catalogs are implementation specs for external catalog systems. They describe how a specific catalog system (such as Apache Polaris, Unity Catalog, or Apache Hive Metastore) integrates with Lance. Each implementation defines:

- How catalog objects map to Lance Namespace concepts
- How to identify Lance tables within the catalog
- How each Namespace Client operation is fulfilled by the catalog

## Available Catalogs

Implementation specs are maintained in the [lance-namespace-impls](https://github.com/lance-format/lance-namespace-impls) repository. Supported catalogs include:

- **Apache Polaris**: Integration with Polaris Catalog for multi-engine governance
- **Unity Catalog**: Integration with Databricks Unity Catalog
- **Apache Hive Metastore**: Integration with Hive Metastore for legacy warehouse compatibility
- **Apache Iceberg REST Catalog**: Integration with Iceberg's REST Catalog protocol
- **AWS Glue Data Catalog**: Integration with AWS Glue for cloud-native deployments

## Contributing

Catalog implementations can be owned by external parties without needing to go through the Lance community voting process to be adopted. Anyone can provide additional implementation specs outside the core Lance Namespace spec.

To contribute a new catalog implementation, follow the [Implementation Spec Template](template.md) which defines the standard structure for describing how a catalog system integrates with the Namespace Client.



================================================
FILE: docs/src/namespace/supported-catalogs/lance-dir.md
================================================
# Lance Directory Catalog

This document describes how the Lance Directory Catalog implements the Lance Namespace Client operations.

## Background

The Lance Directory Catalog is a storage-native catalog that stores tables in a directory structure on any local or remote storage system. For details on the catalog design including V1 (directory listing), V2 (manifest), and compatibility mode, see the [Lance Directory Catalog](../../catalog/dir/index.md) specification.

## Implementation Configuration Properties

The Lance Directory Catalog implementation accepts the following configuration properties:

The **root** property is required and specifies the root directory of the catalog where tables are stored. This can be a local path like `/my/dir` or a cloud storage URI like `s3://bucket/prefix`.

The **manifest_enabled** property controls whether the manifest table is used for tracking tables and namespaces (V2). Defaults to `true`.

The **dir_listing_enabled** property controls whether directory scanning is used for table discovery (V1). Defaults to `true`.

By default, both properties are enabled, which means the implementation operates in [Compatibility Mode](../../catalog/dir/index.md#compatibility-mode).

Properties with the **storage.** prefix are passed directly to the underlying Lance ObjectStore after removing the prefix. For example, `storage.region` becomes `region` when passed to the storage layer.

## Object Mapping

### Namespace

The **root namespace** is the root directory specified by the `root` configuration property. This is the base path where all tables are stored.

A **child namespace** is a logical container tracked in the manifest table. Child namespaces are only supported in V2; V1 treats the root directory as a flat namespace containing only tables. Child namespaces do not correspond to physical subdirectories.

The **namespace identifier** is a list of strings representing the namespace path. For example, a namespace `["prod", "analytics"]` is serialized to `prod$analytics` when stored in the manifest table's `object_id` column.

**Namespace properties** are stored as JSON in the `metadata` column of the manifest table. This is only available in V2.

### Table

A **table** is a subdirectory containing Lance table data. The directory must contain valid Lance format files including the `_versions/` directory with version manifests.

The **table identifier** is a list of strings representing the namespace path followed by the table name. For example, a table `["prod", "analytics", "users"]` represents a table named `users` in namespace `["prod", "analytics"]`. This is serialized to `prod$analytics$users` when stored in the manifest table's `object_id` column.

The **table location** depends on the mode and namespace level:

- In V1 (root namespace only), tables are stored as `<table_name>.lance` directories
- In V2 with `dir_listing_enabled=true` and an empty namespace (root level), tables use the `<table_name>.lance` naming convention for backward compatibility
- In V2 for child namespaces, or when `dir_listing_enabled=false`, tables are stored as `<hash>_<object_id>` directories where hash provides entropy for object store throughput

**Table properties** are stored in Lance table metadata and can be accessed via the Lance SDK.

## Lance Table Identification

In a Directory Catalog, a Lance table is identified differently depending on the mode:

In **V1**, a Lance table is any directory with the `.lance` suffix (e.g., `users.lance/`). The directory must contain valid Lance table data to be usable. Only single-level table identifiers (e.g., `["users"]`) are supported in this mode.

In **V2**, a Lance table is identified by a row in the manifest table with `object_type="table"`. The row's `location` field points to the Lance table directory. Multi-level table identifiers (e.g., `["prod", "analytics", "users"]`) are supported.

A valid Lance table directory must be non-empty.

## Basic Operations

### CreateNamespace

This operation is only supported in V2. V1 does not support explicit namespace creation since it uses a flat directory structure.

The implementation creates a new namespace using a merge-insert operation on the manifest table:

1. Validate the parent namespace exists (if not creating at root level)
2. Merge-insert a new row into the manifest table with:
     - `object_id` set to the namespace identifier (e.g., `prod$analytics`)
     - `object_type` set to `"namespace"`
     - `metadata` containing the namespace properties as JSON
     - `created_at` set to the current timestamp

   Primary-key deduplication on `object_id` ensures no duplicate rows are inserted. If a namespace with the same identifier already exists, the operation fails.

**Error Handling:**

If a namespace with the same identifier already exists, return error code `2` (NamespaceAlreadyExists).

If the parent namespace does not exist (for nested namespaces), return error code `1` (NamespaceNotFound).

If the identifier format is invalid, return error code `13` (InvalidInput).

### ListNamespaces

This operation lists child namespaces within a parent namespace.

In **V1**, this operation returns an empty list since namespaces are not supported.

In **V2**, the implementation queries the manifest table:

1. Query for rows where `object_type = "namespace"`
2. Filter to rows where `object_id` starts with the parent namespace prefix
3. Further filter to rows where `object_id` has exactly one more level than the parent
4. Return the list of namespace names (the last component of each identifier)

**Error Handling:**

If the parent namespace does not exist (V2 only), return error code `1` (NamespaceNotFound).

### DescribeNamespace

This operation is only supported in V2 and returns namespace metadata.

The implementation:

1. Query the manifest table for the row with the matching `object_id`
2. Parse the `metadata` column as JSON
3. Return the namespace name and properties

**Error Handling:**

If the namespace does not exist, return error code `1` (NamespaceNotFound).

### DropNamespace

This operation is only supported in V2 and removes a namespace.

The implementation:

1. Check that the namespace exists in the manifest table
2. Query for any child namespaces or tables with identifiers starting with this namespace's prefix
3. If any children exist, the operation fails
4. Delete the namespace row from the manifest table using the `object_id` primary key

**Error Handling:**

If the namespace does not exist, return error code `1` (NamespaceNotFound).

If the namespace contains tables or child namespaces, return error code `3` (NamespaceNotEmpty).

### DeclareTable

This operation declares a new Lance table, reserving the table name and location without creating actual data files.

The implementation:

1. Validate the parent namespace exists (in V2)
2. Determine the table location:
     - In V1: `<root>/<table_name>.lance`
     - In V2 with `dir_listing_enabled=true` at root level: `<root>/<table_name>.lance`
     - In V2 for child namespaces or with `dir_listing_enabled=false`: `<root>/<hash>_<object_id>/`
3. Create a `.lance-reserved` file at the location to mark the table's existence
4. In V2, merge-insert a row into the manifest table with:
     - `object_id` set to the table identifier
     - `object_type` set to `"table"`
     - `location` set to the table directory path

   Primary-key deduplication on `object_id` ensures no duplicate rows are inserted. If a table with the same identifier already exists, the operation fails.

**Error Handling:**

If the parent namespace does not exist, return error code `1` (NamespaceNotFound).

If a table with the same identifier already exists, return error code `5` (TableAlreadyExists).

If there is a concurrent creation attempt, return error code `14` (ConcurrentModification).

### ListTables

This operation lists tables within a namespace.

In **V1**:

1. List all entries in the root directory
2. Filter to directories matching the `*.lance` pattern
3. Return the table names (directory names without the `.lance` suffix)

In **V2**:

1. Query the manifest table for rows where `object_type = "table"`
2. Filter to rows where `object_id` starts with the namespace prefix
3. Further filter to rows where `object_id` has exactly one more level than the namespace
4. Return the list of table names

When **both V1 and V2 are enabled** (the default [Compatibility Mode](../../catalog/dir/index.md#compatibility-mode)),
the implementation performs both queries and merges results, with manifest entries taking precedence when duplicates exist.

**Error Handling:**

If the namespace does not exist (V2 only), return error code `1` (NamespaceNotFound).

### DescribeTable

This operation returns table metadata including schema, version, and properties.

The implementation:

1. Locate the table:
     - In V1, check for the `<table_name>.lance` directory
     - In V2, query the manifest table for the table location
     - When both V1 and V2 are enabled (the default [Compatibility Mode](../../catalog/dir/index.md#compatibility-mode)),
       first check the manifest table, then fall back to checking the `.lance` directory
2. Open the Lance table using the Lance SDK
3. Read the table metadata and return:
     - `name`: The table name
     - `schema`: The Arrow schema of the table
     - `version`: The current version number
     - `location`: The table directory path

**Error Handling:**

If the parent namespace does not exist, return error code `1` (NamespaceNotFound).

If the table does not exist, return error code `4` (TableNotFound).

If a specific version is requested and does not exist, return error code `11` (TableVersionNotFound).

### DeregisterTable

This operation deregisters a table from the catalog while preserving its data on storage. The table files remain at their storage location and can be re-registered later using RegisterTable.

In **V1**:

1. Locate the table by checking for the `<table_name>.lance` directory
2. Verify the table exists and is not already deregistered
3. Create a `.lance-deregistered` marker file inside the table directory
4. Return the table location for reference

The marker file approach ensures that:
- Table data remains intact at its original location
- The table is excluded from `ListTables` results
- The table returns `TableNotFound` for `DescribeTable` and `TableExists` operations
- The table can be re-registered by removing the marker file and calling `RegisterTable`
- `DropTable` still works on deregistered tables (removes both data and marker file)

In **V2**:

1. Locate the table by querying the manifest table for the table location
2. Remove the table row from the manifest table using the `object_id` primary key
3. Keep the table files at the storage location
4. Return the table location and properties for reference

When **both V1 and V2 are enabled** (the default [Compatibility Mode](../../catalog/dir/index.md#compatibility-mode)),
first check the manifest table, then fall back to checking the `.lance` directory.
If found in manifest, follow V2 behavior; otherwise follow V1 behavior.

**Error Handling:**

If the parent namespace does not exist, return error code `1` (NamespaceNotFound).

If the table does not exist or is already deregistered, return error code `4` (TableNotFound).

## Additional Operations

### DropTable

This operation removes a table and its data.

In **V1**:

1. Locate the table by checking for the `<table_name>.lance` directory
2. Delete the table directory and all its contents from storage
3. If deletion fails midway (directory is still non-empty), the drop has failed and should be retried

In **V2**:

1. Locate the table by querying the manifest table for the table location
2. Remove the table row from the manifest table using the `object_id` primary key
3. Delete the table directory and all its contents from storage
   (failure here does not affect the success of the drop since the table is no longer reachable)

When **both V1 and V2 are enabled** (the default [Compatibility Mode](../../catalog/dir/index.md#compatibility-mode)),
first check the manifest table, then fall back to checking the `.lance` directory.
If found in manifest, follow V2 behavior; otherwise follow V1 behavior.

**Error Handling:**

If the parent namespace does not exist, return error code `1` (NamespaceNotFound).

If the table does not exist, return error code `4` (TableNotFound).

If there is a file system permission error, return error code `15` (PermissionDenied).

If there is an unexpected I/O error, return error code `18` (Internal).

### CreateTableVersion

This operation creates a new version entry for a table. It supports `put_if_not_exists` semantics.

When **table version management is not enabled**:

1. Resolve the table location
2. Parse the staging manifest path from the request
3. Determine the final manifest path based on the naming scheme (V1 or V2)
4. Copy the staging manifest to the final path in the `_versions/` directory using `put_if_not_exists` semantics
5. Delete the staging manifest file
6. Return the created version info including the final manifest path

When **table version management is enabled** (V2 with `table_version_management=true` in `__manifest` metadata), the directory catalog acts as an external manifest store. The commit process follows these steps:

1. **Stage manifest in object storage**: The caller writes the new manifest to a staging path (e.g., `{table_location}/_versions/{version}.manifest-{uuid}`). This staged manifest is not yet visible to readers.
2. **Atomically commit to manifest table**: Merge-insert a new row into the `__manifest` table with:
    - `object_id` set to `<table_id>$<version>` (e.g., `users$1` or `ns1$users$1`)
    - `object_type` set to `"table_version"`
    - `metadata` containing the JSON-encoded version metadata including the staging manifest path

   Primary-key deduplication on `object_id` ensures no duplicate rows are inserted. The commit is effectively complete after this step. If this fails, another writer has already committed that version.
3. **Finalize in object storage**: Copy the staged manifest to the standard location (`{table_location}/_versions/{version}.manifest`). This makes it discoverable by readers that do not use the manifest table.
4. **Update manifest table pointer**: Update the `metadata` in the manifest table row to point to the finalized manifest path, synchronizing both systems.

**Error Handling:**

If the table does not exist, return error code `4` (TableNotFound).

If the version already exists, return error code `12` (TableVersionAlreadyExists).

If there is a concurrent creation attempt, return error code `14` (ConcurrentModification).

### BatchCreateTableVersions

This operation atomically creates version entries for multiple tables.

When **table version management is not enabled**, this operation iterates through each entry and calls `CreateTableVersion` for each one. Atomicity is not guaranteed.

When **table version management is enabled**, the batch commit process follows these steps:

1. **Stage manifests in object storage**: For each entry, the caller writes the new manifest to a staging path (e.g., `{table_location}/_versions/{version}.manifest-{uuid}`).
2. **Atomically commit to manifest table**: Merge-insert all version rows into the `__manifest` table in a single atomic commit, each with:
    - `object_id` set to `<table_id>$<version>`
    - `object_type` set to `"table_version"`
    - `metadata` containing the JSON-encoded version metadata including the staging manifest path

   Primary-key deduplication on `object_id` ensures no duplicate rows are inserted. The commit is effectively complete after this step. If any version already exists, the entire batch fails.
3. **Finalize in object storage**: For each entry, copy the staged manifest to the standard location.
4. **Update manifest table pointers**: Update the `metadata` in each manifest table row to point to the finalized manifest paths.

**Error Handling:**

If any table does not exist, return error code `4` (TableNotFound).

If any version already exists, return error code `12` (TableVersionAlreadyExists).

If there is a concurrent modification, return error code `14` (ConcurrentModification).

### ListTableVersions

This operation lists version entries for a table.

When **table version management is not enabled**:

1. Resolve the table location
2. List all files in the `_versions/` directory
3. Parse version numbers from manifest filenames (handling both V1 and V2 naming schemes)
4. Extract metadata from file attributes (size, e_tag, last_modified timestamp)
5. Sort results by version number (descending if `descending=true`)
6. Apply pagination using `page_token` and `limit`

When **table version management is enabled**:

1. Query the manifest table for rows where:
    - `object_type = "table_version"`
    - `object_id` starts with `<table_id>$`
2. Parse the version number from each `object_id`
3. Parse the `metadata` column as JSON to extract version details
4. Sort results by version number (descending if `descending=true`)
5. Apply pagination using `page_token` and `limit`

**Error Handling:**

If the table does not exist, return error code `4` (TableNotFound).

### DescribeTableVersion

This operation retrieves details for a specific table version.

When **table version management is not enabled**:

1. Resolve the table location
2. Open the Lance dataset at the specified version
3. Read the manifest file to extract version metadata
4. Return the version information including manifest_path, manifest_size, e_tag, timestamp_millis, and metadata

When **table version management is enabled**, the read process validates and synchronizes the manifest:

1. **Query manifest table**: Retrieve the manifest path for the requested version from the row with `object_id = <table_id>$<version>`. If the path matches the expected path based on the naming scheme, synchronization is complete.
2. **Synchronize to object storage**: If the manifest path does not match the expected path based on the naming scheme (i.e., it is a staging path), copy the staged manifest to its final location (`{table_location}/_versions/{version}.manifest`). This is an idempotent operation.
3. **Update manifest table**: Update the `metadata` in the manifest table row to reflect the finalized path for future readers.
4. **Return version information**: Return the version information with the finalized manifest path, or error if synchronization fails.

**Error Handling:**

If the table does not exist, return error code `4` (TableNotFound).

If the version does not exist, return error code `11` (TableVersionNotFound).

### BatchDeleteTableVersions

This operation deletes multiple version entries for a table.

When **table version management is not enabled**:

1. Resolve the table location
2. Delete the manifest files in the `_versions/` directory for each specified version
3. Return the count of deleted versions

When **table version management is enabled**:

1. Delete the manifest files in the `_versions/` directory for each specified version
2. Delete rows from the manifest table using the `object_id` primary key for each specified version
3. Return the count of deleted versions

**Error Handling:**

If the table does not exist, return error code `4` (TableNotFound).

If any specified version does not exist, the operation may either skip it silently or return error code `11` (TableVersionNotFound), depending on the `ignore_missing` parameter.



================================================
FILE: docs/src/namespace/supported-catalogs/lance-rest.md
================================================
# Lance REST Catalog

This document describes how the Lance REST Catalog implements the Lance Namespace Client operations.

## Background

The Lance REST Catalog provides access to Lance tables via a REST API. For details on the API design, endpoints, and data models, see the [Lance REST Catalog](../../catalog/rest/index.md) specification.

## Implementation Configuration Properties

The Lance REST Catalog implementation accepts the following configuration properties:

The **uri** property is required and specifies the URI endpoint for the REST API, for example `https://api.example.com/lance`.

The **delimiter** property specifies the delimiter used to parse object string identifiers in REST routes. Defaults to `$`. Other examples include `::` or `__delim__`.

Properties with the **headers.** prefix are passed as HTTP headers with every request to the REST server after removing the prefix. For example, `headers.Authorization` becomes the `Authorization` header. Common configurations include `headers.Authorization` for authentication tokens, `headers.X-API-Key` for API key authentication, and `headers.X-Request-ID` for request tracking.

## Object Mapping

### Namespace

The **root namespace** is represented by the delimiter character itself in REST routes (e.g., `$`). All REST API calls are made relative to the base URI.

A **child namespace** is managed by the REST server and accessed via namespace routes. The server is responsible for storing and organizing namespace metadata.

The **namespace identifier** is a list of strings representing the namespace path. For example, a namespace `["prod", "analytics"]` is serialized to `prod$analytics` in the REST route path using the configured delimiter (default `$`).

**Namespace properties** are managed by the REST server and accessed via the DescribeNamespace operation.

### Table

A **table** is managed by the REST server. The server handles table storage, versioning, and metadata management.

The **table identifier** is a list of strings representing the namespace path followed by the table name. For example, a table `["prod", "analytics", "users"]` represents a table named `users` in namespace `["prod", "analytics"]`. This is serialized to `prod$analytics$users` in the REST route path using the configured delimiter.

The **table location** is managed by the REST server and returned in the DescribeTable response. This location points to where the Lance table data is stored (e.g., an S3 path).

**Table properties** are managed by the REST server and accessed via table operations.

## Lance Table Identification

In a REST Catalog, the server is responsible for managing Lance tables. The client identifies tables by their string identifier and delegates all table operations to the server.

The server implementation must ensure that:

- Tables are stored as valid Lance table directories on the underlying storage
- The `location` field in DescribeTable response points to the Lance table root directory
- Table properties include any Lance-specific metadata required by the Lance SDK

## Basic Operations

### CreateNamespace

Creates a new namespace.

**HTTP Request:**

```
POST /v1/namespace/{id}/create
Content-Type: application/json
```

The request body contains optional namespace properties:

```json
{
  "properties": {
    "description": "Production analytics namespace"
  }
}
```

The implementation:

1. Parse the namespace identifier from the route path `{id}`
2. Validate the request body format
3. Check if the parent namespace exists (for nested namespaces)
4. Check if a namespace with this identifier already exists
5. Create the namespace in the server's storage
6. Return the created namespace details

**Response:**

```json
{
  "name": "analytics",
  "properties": {
    "description": "Production analytics namespace"
  }
}
```

**Error Handling:**

If the request body is malformed, return HTTP `400 Bad Request` with error code `13` (InvalidInput).

If a namespace with the same identifier already exists, return HTTP `409 Conflict` with error code `2` (NamespaceAlreadyExists).

If the parent namespace does not exist, return HTTP `404 Not Found` with error code `1` (NamespaceNotFound).

### ListNamespaces

Lists child namespaces within a parent namespace.

**HTTP Request:**

```
GET /v1/namespace/{id}/list?page_token=xxx&limit=100
```

The `page_token` and `limit` query parameters support pagination.

The implementation:

1. Parse the parent namespace identifier from the route path `{id}`
2. Validate the parent namespace exists
3. Query the server's storage for child namespaces
4. Apply pagination using `page_token` and `limit`
5. Return the list of namespace names

**Response:**

```json
{
  "namespaces": ["analytics", "ml", "reporting"],
  "next_page_token": "abc123"
}
```

The `next_page_token` field is only present if there are more results.

**Error Handling:**

If the parent namespace does not exist, return HTTP `404 Not Found` with error code `1` (NamespaceNotFound).

### DescribeNamespace

Returns namespace metadata.

**HTTP Request:**

```
POST /v1/namespace/{id}/describe
Content-Type: application/json
```

The request body is empty:

```json
{}
```

The implementation:

1. Parse the namespace identifier from the route path `{id}`
2. Look up the namespace in the server's storage
3. Return the namespace name and properties

**Response:**

```json
{
  "name": "analytics",
  "properties": {
    "description": "Production analytics namespace",
    "created_at": "2024-01-15T10:30:00Z"
  }
}
```

**Error Handling:**

If the namespace does not exist, return HTTP `404 Not Found` with error code `1` (NamespaceNotFound).

### DropNamespace

Removes a namespace.

**HTTP Request:**

```
POST /v1/namespace/{id}/drop
Content-Type: application/json
```

The request body is empty:

```json
{}
```

The implementation:

1. Parse the namespace identifier from the route path `{id}`
2. Check that the namespace exists
3. Check that the namespace is empty (no child namespaces or tables)
4. Delete the namespace from the server's storage

**Response:**

```json
{}
```

**Error Handling:**

If the namespace does not exist, return HTTP `404 Not Found` with error code `1` (NamespaceNotFound).

If the namespace contains tables or child namespaces, return HTTP `409 Conflict` with error code `3` (NamespaceNotEmpty).

### DeclareTable

Declares a new Lance table, reserving the table name and location without creating actual data files.

**HTTP Request:**

```
POST /v1/table/{id}/declare
Content-Type: application/json
```

The request body contains an optional location:

```json
{
  "location": "s3://bucket/data/users.lance"
}
```

The implementation:

1. Parse the table identifier from the route path `{id}`
2. Extract the parent namespace from the identifier
3. Validate the parent namespace exists
4. Check if a table with this identifier already exists
5. Determine the table location (use provided location or generate one)
6. Reserve the table in the server's storage
7. Register the table in the namespace

**Response:**

```json
{
  "location": "s3://bucket/data/users.lance",
  "storage_options": {
    "aws_access_key_id": "...",
    "aws_secret_access_key": "..."
  }
}
```

**Error Handling:**

If the parent namespace does not exist, return HTTP `404 Not Found` with error code `1` (NamespaceNotFound).

If a table with the same identifier already exists, return HTTP `409 Conflict` with error code `5` (TableAlreadyExists).

If there is a concurrent creation attempt, return HTTP `409 Conflict` with error code `14` (ConcurrentModification).

### ListTables

Lists tables within a namespace.

**HTTP Request:**

```
GET /v1/namespace/{id}/table/list?page_token=xxx&limit=100
```

The `page_token` and `limit` query parameters support pagination.

The implementation:

1. Parse the namespace identifier from the route path `{id}`
2. Validate the namespace exists
3. Query the server's storage for tables in the namespace
4. Apply pagination using `page_token` and `limit`
5. Return the list of table names

**Response:**

```json
{
  "tables": ["users", "orders", "products"],
  "next_page_token": "def456"
}
```

The `next_page_token` field is only present if there are more results.

**Error Handling:**

If the namespace does not exist, return HTTP `404 Not Found` with error code `1` (NamespaceNotFound).

### DescribeTable

Returns table metadata including schema and version.

**HTTP Request:**

```
POST /v1/table/{id}/describe
Content-Type: application/json
```

The request body can optionally specify a version:

```json
{
  "version": 5
}
```

The implementation:

1. Parse the table identifier from the route path `{id}`
2. Extract the parent namespace from the identifier
3. Validate the parent namespace exists
4. Look up the table in the server's storage
5. If `version` is specified, retrieve that specific version's metadata
6. Return the table metadata

**Response:**

```json
{
  "name": "users",
  "location": "s3://bucket/data/users.lance",
  "schema": {
    "fields": [
      {"name": "id", "type": {"name": "int64"}, "nullable": false},
      {"name": "name", "type": {"name": "utf8"}, "nullable": true}
    ]
  },
  "version": 5
}
```

**Error Handling:**

If the parent namespace does not exist, return HTTP `404 Not Found` with error code `1` (NamespaceNotFound).

If the table does not exist, return HTTP `404 Not Found` with error code `4` (TableNotFound).

If the specified version does not exist, return HTTP `404 Not Found` with error code `11` (TableVersionNotFound).

### DeregisterTable

Deregisters a table from the catalog while preserving its data on storage. The table metadata is removed from the catalog but the table files remain at their storage location.

**HTTP Request:**

```
POST /v1/table/{id}/deregister
Content-Type: application/json
```

The request body is empty:

```json
{}
```

The implementation:

1. Parse the table identifier from the route path `{id}`
2. Extract the parent namespace from the identifier
3. Validate the parent namespace exists
4. Look up the table in the server's storage
5. Remove the table registration from the catalog
6. Return the table location and properties for reference

**Response:**

```json
{
  "location": "s3://bucket/data/users.lance",
  "properties": {
    "created_at": "2024-01-15T10:30:00Z"
  }
}
```

**Error Handling:**

If the parent namespace does not exist, return HTTP `404 Not Found` with error code `1` (NamespaceNotFound).

If the table does not exist, return HTTP `404 Not Found` with error code `4` (TableNotFound).

## Additional Operations

The REST Catalog supports all operations defined in the [Lance Namespace Client spec](../operations/index.md). Each operation follows the same HTTP request/response pattern as the basic operations above.

### DropTable

Removes a table and its data.

**HTTP Request:**

```
POST /v1/table/{id}/drop
Content-Type: application/json
```

The request body is empty:

```json
{}
```

The implementation:

1. Parse the table identifier from the route path `{id}`
2. Extract the parent namespace from the identifier
3. Validate the parent namespace exists
4. Look up the table in the server's storage
5. Delete the table data from storage
6. Remove the table registration from the catalog

**Response:**

```json
{}
```

**Error Handling:**

If the parent namespace does not exist, return HTTP `404 Not Found` with error code `1` (NamespaceNotFound).

If the table does not exist, return HTTP `404 Not Found` with error code `4` (TableNotFound).

If there is a storage permission error, return HTTP `403 Forbidden` with error code `15` (PermissionDenied).

If there is an unexpected server error, return HTTP `500 Internal Server Error` with error code `18` (Internal).

### RegisterTable

Registers an existing Lance table at a given location.

**HTTP Request:**

```
POST /v1/table/{id}/register
Content-Type: application/json
```

```json
{
  "location": "s3://bucket/data/users.lance"
}
```

**Error Handling:**

If the parent namespace does not exist, return HTTP `404 Not Found` with error code `1` (NamespaceNotFound).

If a table with the same identifier already exists, return HTTP `409 Conflict` with error code `5` (TableAlreadyExists).

If the location does not contain a valid Lance table, return HTTP `400 Bad Request` with error code `13` (InvalidInput).

### RenameTable

Renames a table, optionally moving it to a different namespace.

**HTTP Request:**

```
POST /v1/table/{id}/rename
Content-Type: application/json
```

```json
{
  "new_id": ["new_namespace", "new_table_name"]
}
```

**Error Handling:**

If the source table does not exist, return HTTP `404 Not Found` with error code `4` (TableNotFound).

If a table with the new identifier already exists, return HTTP `409 Conflict` with error code `5` (TableAlreadyExists).

If the target namespace does not exist, return HTTP `404 Not Found` with error code `1` (NamespaceNotFound).

### CreateTableVersion

Creates a new version entry for a table.

**HTTP Request:**

```
POST /v1/table/{id}/version/create
Content-Type: application/json
```

```json
{
  "version": 2,
  "manifest_path": "s3://bucket/data/users.lance/_versions/staging-uuid.manifest",
  "naming_scheme": "V2"
}
```

**Error Handling:**

If the table does not exist, return HTTP `404 Not Found` with error code `4` (TableNotFound).

If the version already exists, return HTTP `409 Conflict` with error code `12` (TableVersionAlreadyExists).

### ListTableVersions

Lists version entries for a table.

**HTTP Request:**

```
GET /v1/table/{id}/version/list?descending=true&limit=100
```

**Error Handling:**

If the table does not exist, return HTTP `404 Not Found` with error code `4` (TableNotFound).

### DescribeTableVersion

Retrieves details for a specific table version.

**HTTP Request:**

```
POST /v1/table/{id}/version/describe
Content-Type: application/json
```

```json
{
  "version": 2
}
```

**Error Handling:**

If the table does not exist, return HTTP `404 Not Found` with error code `4` (TableNotFound).

If the version does not exist, return HTTP `404 Not Found` with error code `11` (TableVersionNotFound).

### BatchCreateTableVersions

Atomically creates version entries for multiple tables.

**HTTP Request:**

```
POST /v1/table/version/batch-create
Content-Type: application/json
```

```json
{
  "entries": [
    {
      "id": ["namespace", "table1"],
      "version": 2,
      "manifest_path": "s3://bucket/data/table1.lance/_versions/staging-uuid.manifest"
    },
    {
      "id": ["namespace", "table2"],
      "version": 3,
      "manifest_path": "s3://bucket/data/table2.lance/_versions/staging-uuid.manifest"
    }
  ]
}
```

**Error Handling:**

If any table does not exist, return HTTP `404 Not Found` with error code `4` (TableNotFound).

If any version already exists, return HTTP `409 Conflict` with error code `12` (TableVersionAlreadyExists).

### BatchDeleteTableVersions

Deletes multiple version entries for a table.

**HTTP Request:**

```
POST /v1/table/{id}/version/batch-delete
Content-Type: application/json
```

```json
{
  "versions": [1, 2, 3]
}
```

**Error Handling:**

If the table does not exist, return HTTP `404 Not Found` with error code `4` (TableNotFound).

If any specified version does not exist and `ignore_missing` is false, return HTTP `404 Not Found` with error code `11` (TableVersionNotFound).

### NamespaceExists

Checks if a namespace exists.

**HTTP Request:**

```
POST /v1/namespace/{id}/exists
```

### TableExists

Checks if a table exists.

**HTTP Request:**

```
POST /v1/table/{id}/exists
```

### ListAllTables

Lists all tables across all namespaces.

**HTTP Request:**

```
GET /v1/table/list?page_token=xxx&limit=100
```

### RestoreTable

Restores a table to a previous version.

**HTTP Request:**

```
POST /v1/table/{id}/restore
Content-Type: application/json
```

```json
{
  "version": 5
}
```

### CreateTable

Creates a new table with initial data.

For REST namespace, `CreateTableRequest` fields are passed as follows:

- `id`: path parameter
- `mode`: query parameter
- `properties`: a single JSON-encoded query parameter such as
  `properties={"user":"alice","team":"eng"}`; these are business logic properties managed
  by the namespace implementation outside Lance context
- `storage_options`: a single JSON-encoded query parameter such as
  `storage_options={"aws_region":"us-east-1","timeout":"30s"}`; these configure write-time
  overrides for data and metadata written during table creation

**HTTP Request:**

```
POST /v1/table/{id}/create
Content-Type: application/vnd.apache.arrow.stream
```

**Response:**

```json
{
  "location": "s3://bucket/data/users.lance",
  "version": 1,
  "storage_options": {
    "aws_region": "us-east-1"
  },
  "properties": {
    "user": "alice"
  }
}
```

### GetTableStats

Returns statistics for a table.

**HTTP Request:**

```
POST /v1/table/{id}/stats
```

### UpdateTableSchemaMetadata

Updates schema-level metadata for a table.

**HTTP Request:**

```
POST /v1/table/{id}/schema/metadata
Content-Type: application/json
```

### AlterTableAddColumns

Adds new columns to a table.

**HTTP Request:**

```
POST /v1/table/{id}/add_columns
Content-Type: application/json
```

### AlterTableAlterColumns

Modifies existing columns in a table.

**HTTP Request:**

```
POST /v1/table/{id}/alter_columns
Content-Type: application/json
```

### AlterTableBackfillColumns

Triggers an async backfill job for a computed column.

**HTTP Request:**

```
POST /v1/table/{id}/backfill_column
Content-Type: application/json
```

### AlterTableDropColumns

Removes columns from a table.

**HTTP Request:**

```
POST /v1/table/{id}/drop_columns
Content-Type: application/json
```

### RefreshMaterializedView

Triggers an async materialized view refresh.

**HTTP Request:**

```
POST /v1/materialized_view/{id}/refresh
Content-Type: application/json
```

### InsertIntoTable

Inserts data into a table.

**HTTP Request:**

```
POST /v1/table/{id}/insert
Content-Type: application/json
```

### MergeInsertIntoTable

Performs a merge insert (upsert) operation.

**HTTP Request:**

```
POST /v1/table/{id}/merge-insert
Content-Type: application/json
```

### UpdateTable

Updates rows in a table.

**HTTP Request:**

```
POST /v1/table/{id}/update
Content-Type: application/json
```

### DeleteFromTable

Deletes rows from a table.

**HTTP Request:**

```
POST /v1/table/{id}/delete
Content-Type: application/json
```

### QueryTable

Queries data from a table.

**HTTP Request:**

```
POST /v1/table/{id}/query
Content-Type: application/json
```

### CountTableRows

Counts rows in a table.

**HTTP Request:**

```
POST /v1/table/{id}/count
Content-Type: application/json
```

### ExplainTableQueryPlan

Returns the query execution plan.

**HTTP Request:**

```
POST /v1/table/{id}/query/explain
Content-Type: application/json
```

### AnalyzeTableQueryPlan

Analyzes the query execution plan with statistics.

**HTTP Request:**

```
POST /v1/table/{id}/query/analyze
Content-Type: application/json
```

### CreateTableIndex

Creates a vector index on a table.

**HTTP Request:**

```
POST /v1/table/{id}/index/create
Content-Type: application/json
```

### CreateTableScalarIndex

Creates a scalar index on a table.

**HTTP Request:**

```
POST /v1/table/{id}/index/create-scalar
Content-Type: application/json
```

### ListTableIndices

Lists all indices on a table.

**HTTP Request:**

```
GET /v1/table/{id}/index/list
```

### DescribeTableIndexStats

Returns statistics for a table index.

**HTTP Request:**

```
POST /v1/table/{id}/index/{index_name}/stats
```

### DropTableIndex

Removes an index from a table.

**HTTP Request:**

```
POST /v1/table/{id}/index/{index_name}/drop
```

### ListTableTags

Lists all tags for a table.

**HTTP Request:**

```
GET /v1/table/{id}/tag/list
```

### GetTableTagVersion

Gets the version associated with a tag.

**HTTP Request:**

```
POST /v1/table/{id}/tag/{tag_name}/describe
```

### CreateTableTag

Creates a new tag for a table version.

**HTTP Request:**

```
POST /v1/table/{id}/tag/create
Content-Type: application/json
```

### DeleteTableTag

Deletes a tag from a table.

**HTTP Request:**

```
POST /v1/table/{id}/tag/{tag_name}/delete
```

### UpdateTableTag

Updates a tag to point to a different version.

**HTTP Request:**

```
POST /v1/table/{id}/tag/{tag_name}/update
Content-Type: application/json
```

### DescribeTransaction

Returns details about a transaction.

**HTTP Request:**

```
POST /v1/transaction/{id}/describe
```

### AlterTransaction

Modifies a transaction's state.

**HTTP Request:**

```
POST /v1/transaction/{id}/alter
Content-Type: application/json
```

## Error Response Format

All error responses follow the JSON error response model based on [RFC-7807](https://datatracker.ietf.org/doc/html/rfc7807).

The response body contains an [ErrorResponse](../operations/models/ErrorResponse.md) with a `code` field containing the Lance Namespace error code. See [Error Handling](../operations/errors.md) for the complete list of error codes.

**Example error response:**

```json
{
  "error": "Table 'users' not found in namespace 'production'",
  "code": 4,
  "detail": "java.lang.RuntimeException: Table not found\n\tat com.example.TableService.describe(TableService.java:42)\n\tat ...",
  "instance": "/v1/table/production$users/describe"
}
```

The `detail` field contains detailed error information such as stack traces for debugging purposes.

## Error Code to HTTP Status Mapping

REST Catalog implementations must map Lance error codes to HTTP status codes as follows:

- Error code `0` (Unsupported) maps to HTTP `406 Not Acceptable`
- Error codes `1`, `4`, `6`, `8`, `10`, `11`, `12` (not found errors) map to HTTP `404 Not Found`
- Error codes `2`, `3`, `5`, `7`, `9`, `14`, `19` (conflict errors) map to HTTP `409 Conflict`
- Error codes `13`, `20` (input validation errors) map to HTTP `400 Bad Request`
- Error code `15` (PermissionDenied) maps to HTTP `403 Forbidden`
- Error code `16` (Unauthenticated) maps to HTTP `401 Unauthorized`
- Error code `17` (ServiceUnavailable) maps to HTTP `503 Service Unavailable`
- Error code `18` (Internal) maps to HTTP `500 Internal Server Error`
- Error code `21` (Throttling) maps to HTTP `429 Too Many Requests`



================================================
FILE: docs/src/namespace/supported-catalogs/template.md
================================================
# Catalog Integration Template

This template defines the standard structure for Lance Catalog implementation specs.
Each implementation spec describes how a specific catalog system integrates with the Lance Namespace Client.

## Required Sections

### 1. Background

Provide a brief introduction to the catalog system being integrated:

- What the catalog system is and its purpose
- Link to the catalog spec for detailed design information
- Any important context for understanding the implementation

### 2. Namespace Implementation Configuration Properties

List all configuration properties accepted by the namespace implementation:

- Required vs optional properties
- Property descriptions and default values
- Prefix-based properties (e.g., `storage.*`, `headers.*`)

### 3. Object Mapping

Describe how objects in the catalog system map to Lance Namespace concepts using paragraphs (not tables):

**Namespace Mapping:**
- How the root namespace is represented
- How child namespaces are organized
- How namespace identifiers are constructed
- Where namespace properties are stored

**Table Mapping:**
- How tables are represented
- How table identifiers are constructed
- Where table data is stored (location)
- Where table properties are stored

### 4. Lance Table Identification

Describe how to determine if a table in the catalog is a Lance table:

- Required properties, markers, or naming conventions
- Storage location requirements
- How the implementation verifies table validity

### 5. Basic Operations

For each of the 8 recommended basic operations, provide a detailed subsection. Each operation subsection should include:

- A brief description of what the operation does
- Step-by-step implementation details
- An **Error Handling** paragraph describing which errors can occur and under what conditions

The 8 basic operations are:

**Namespace Operations:**

- CreateNamespace
- ListNamespaces
- DescribeNamespace
- DropNamespace (only `Restrict` behavior mode required)

**Table Operations:**

- DeclareTable
- ListTables
- DescribeTable (only `load_detailed_metadata=false` required)
- DeregisterTable

**Note:** For basic implementations, DropNamespace only needs to support the `Restrict` behavior mode
(namespace must be empty before dropping). DescribeTable only needs to support `load_detailed_metadata=false`
(only return table `location` without opening the dataset).

### 6. Additional Operations (Optional)

If the implementation supports operations beyond the 8 basic operations, document them in this section.
Each additional operation should follow the same structure as basic operations:

- A brief description of what the operation does
- Step-by-step implementation details
- An **Error Handling** paragraph describing which errors can occur

Common additional operations include:

- DropTable
- RegisterTable
- RenameTable
- Table version operations (CreateTableVersion, ListTableVersions, DescribeTableVersion, BatchCreateTableVersions, BatchDeleteTableVersions)

---

## Template Structure

```markdown
# {Catalog Name}

This document describes how the {Catalog Name} implements the Lance Namespace client spec.

## Background

{Brief description of the catalog system and its purpose}. For details on the catalog design, see the [{Catalog Name} Catalog Spec](link-to-the-spec).

## Namespace Implementation Configuration Properties

The Lance {Catalog Name} namespace implementation accepts the following configuration properties:

The **{property_name}** property is {required/optional} and {description}. {Default value if optional}.

{Additional properties...}

## Object Mapping

### Namespace

The **root namespace** is {description of how root namespace maps}.

A **child namespace** is {description of child namespace representation}.

The **namespace identifier** is {description of identifier format}.

**Namespace properties** are {description of where/how properties are stored}.

### Table

A **table** is {description of table representation}.

The **table identifier** is {description of identifier format}.

The **table location** is {description of where table data is stored}.

**Table properties** are {description of where/how properties are stored}.

## Lance Table Identification

{Paragraph describing how to identify a Lance table in this catalog system}

## Basic Operations

### CreateNamespace

{Brief description of operation}

The implementation:

1. {Step 1}
2. {Step 2}
3. {Step N}

**Error Handling:**

If {condition}, return error code `N` ({ErrorName}).

{Additional error conditions...}

### ListNamespaces

{Same structure as above}

### DescribeNamespace

{Same structure as above}

### DropNamespace

{Same structure as above}

**Note:** Basic implementations only need to support `Restrict` behavior mode.

### DeclareTable

{Same structure as above}

### ListTables

{Same structure as above}

### DescribeTable

{Same structure as above}

**Note:** Basic implementations only need to support `load_detailed_metadata=false` (only return table `location`).

### DeregisterTable

{Same structure as above}

## Additional Operations

{Optional section for operations beyond the 8 basic operations}

### DropTable

{Same structure as basic operations}

### {Other Additional Operations}

{Same structure as basic operations}
```

## Error Handling Guidelines

Each operation's error handling section should describe errors in paragraph form, one paragraph per error condition. Include:

- The condition that triggers the error
- The error code number
- The error name in parentheses

Example:

> If the namespace does not exist, return error code `1` (NamespaceNotFound).
>
> If the namespace contains tables or child namespaces, return error code `3` (NamespaceNotEmpty).

For catalog specs that map to HTTP (like REST), also include the HTTP status code:

> If the namespace does not exist, return HTTP `404 Not Found` with error code `1` (NamespaceNotFound).



================================================
FILE: docs/src/namespace/supported-catalogs/.pages
================================================
title: Supported Catalogs
nav:
  - Overview: index.md
  - Lance Directory Catalog: lance-dir.md
  - Lance REST Catalog: lance-rest.md
  - Template: template.md


