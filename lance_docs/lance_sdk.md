Python API Reference
¶
This section contains the API reference for the Python API of LanceDB. Both synchronous and asynchronous APIs are available.

The general flow of using the API is:

Use lancedb.connect or lancedb.connect_async to connect to a database.
Use the returned lancedb.DBConnection or lancedb.AsyncConnection to create or open tables.
Use the returned lancedb.table.Table or lancedb.AsyncTable to query or modify tables.
Installation
¶

pip install lancedb
The following methods describe the synchronous API client. There is also an asynchronous API client.

Connections (Synchronous)
¶
lancedb.connect
¶

connect(uri: Optional[URI] = None, *, api_key: Optional[str] = None, region: str = 'us-east-1', host_override: Optional[str] = None, read_consistency_interval: Optional[timedelta] = None, request_thread_pool: Optional[Union[int, ThreadPoolExecutor]] = None, client_config: Union[ClientConfig, Dict[str, Any], None] = None, storage_options: Optional[Dict[str, str]] = None, session: Optional[Session] = None, manifest_enabled: bool = False, namespace_client_impl: Optional[str] = None, namespace_client_properties: Optional[Dict[str, str]] = None, namespace_client_pushdown_operations: Optional[List[str]] = None, **kwargs: Any) -> DBConnection
Connect to a LanceDB database.

Parameters:

uri (Optional[URI], default: None ) – The uri of the database. When namespace_client_impl is provided you may omit uri and connect through a namespace client instead.
api_key (Optional[str], default: None ) – If presented, connect to LanceDB cloud. Otherwise, connect to a database on file system or cloud storage. Can be set via environment variable LANCEDB_API_KEY. OAuth configuration is currently supported only by connect_async; synchronous LanceDB Cloud connections require an API key.
region (str, default: 'us-east-1' ) – The region to use for LanceDB Cloud.
host_override (Optional[str], default: None ) – The override url for LanceDB Cloud.
read_consistency_interval (Optional[timedelta], default: None ) – The interval at which to check for updates to the table from other processes. If None, then consistency is not checked. For performance reasons, this is the default. For strong consistency, set this to zero seconds. Then every read will check for updates from other processes. As a compromise, you can set this to a non-zero timedelta for eventual consistency. If more than that interval has passed since the last check, then the table will be checked for updates. Note: this consistency only applies to read operations. Write operations are always consistent.
Stronger consistency is not free. The smaller the interval, the more often each read pays the cost of checking for updates against object storage, raising per-read latency and cost.

client_config (Union[ClientConfig, Dict[str, Any], None], default: None ) – Configuration options for the LanceDB Cloud HTTP client. If a dict, then the keys are the attributes of the ClientConfig class. If None, then the default configuration is used.
storage_options (Optional[Dict[str, str]], default: None ) – Additional options for the storage backend. See available options at https://docs.lancedb.com/storage/
manifest_enabled (bool, default: False ) – When true for local/native connections, use directory namespace manifests as the source of truth for table metadata. Existing directory-listed root tables are migrated into the manifest on access.
session (Optional[Session], default: None ) – (For LanceDB OSS only) A session to use for this connection. Sessions allow you to configure cache sizes for index and metadata caches, which can significantly impact memory use and performance. They can also be re-used across multiple connections to share the same cache state.
namespace_client_impl (str, default: None ) – When provided along with namespace_client_properties, connect returns a namespace-backed connection by delegating to :func:connect_namespace. The value identifies which namespace implementation to load (e.g., "dir" or "rest").
namespace_client_properties (dict, default: None ) – Configuration to pass to the namespace client implementation. Required when namespace_client_impl is set.
namespace_client_pushdown_operations (list[str], default: None ) – Only used when namespace_client_properties is provided. Forwards to :func:connect_namespace to control which operations are executed on the namespace service (e.g., ["QueryTable", "CreateTable"]).
Examples:

For a local directory, provide a path for the database:


>>> import lancedb
>>> db = lancedb.connect("~/.lancedb")
For object storage, use a URI prefix:


>>> db = lancedb.connect("s3://my-bucket/lancedb",
...                      storage_options={"aws_access_key_id": "***"})
For tests and temporary data, use an in-memory database:


>>> db = lancedb.connect("memory://")
In-memory databases are not persisted. Tables are dropped when the last connection or table handle referencing them is closed.

Connect to LanceDB cloud:


>>> db = lancedb.connect("db://my_database", api_key="ldb_...",
...                      client_config={"retry_config": {"retries": 5}})
Connect to a namespace-backed database:


>>> db = lancedb.connect(namespace_client_impl="dir",
...                      namespace_client_properties={"root": "/tmp/ns"})
Returns:

conn ( DBConnection ) – A connection to a LanceDB database.
lancedb.db.DBConnection
¶
Bases: EnforceOverrides

An active LanceDB connection interface.

uri
¶

uri: str
list_namespaces
¶

list_namespaces(namespace_path: Optional[List[str]] = None, page_token: Optional[str] = None, limit: Optional[int] = None) -> ListNamespacesResponse
List immediate child namespace names in the given namespace.

Parameters:

namespace_path (Optional[List[str]], default: None ) – The parent namespace to list namespaces in. Empty list represents root namespace.
page_token (Optional[str], default: None ) – Token for pagination. Use the token from a previous response to get the next page of results.
limit (Optional[int], default: None ) – The maximum number of results to return.
Returns:

ListNamespacesResponse – Response containing namespace names and optional page_token for pagination.
create_namespace
¶

create_namespace(namespace_path: List[str], mode: Optional[str] = None, properties: Optional[Dict[str, str]] = None) -> CreateNamespaceResponse
Create a new namespace.

Parameters:

namespace_path (List[str]) – The namespace identifier to create.
mode (Optional[str], default: None ) – Creation mode - "create" (fail if exists), "exist_ok" (skip if exists), or "overwrite" (replace if exists). Case insensitive.
properties (Optional[Dict[str, str]], default: None ) – Properties to set on the namespace.
Returns:

CreateNamespaceResponse – Response containing the properties of the created namespace.
drop_namespace
¶

drop_namespace(namespace_path: List[str], mode: Optional[str] = None, behavior: Optional[str] = None) -> DropNamespaceResponse
Drop a namespace.

Parameters:

namespace_path (List[str]) – The namespace identifier to drop.
mode (Optional[str], default: None ) – Whether to skip if not exists ("SKIP") or fail ("FAIL"). Case insensitive.
behavior (Optional[str], default: None ) – Whether to restrict drop if not empty ("RESTRICT") or cascade ("CASCADE"). Case insensitive.
Returns:

DropNamespaceResponse – Response containing properties and transaction_id if applicable.
describe_namespace
¶

describe_namespace(namespace_path: List[str]) -> DescribeNamespaceResponse
Describe a namespace.

Parameters:

namespace_path (List[str]) – The namespace identifier to describe.
Returns:

DescribeNamespaceResponse – Response containing the namespace properties.
list_tables
¶

list_tables(namespace_path: Optional[List[str]] = None, page_token: Optional[str] = None, limit: Optional[int] = None) -> ListTablesResponse
List all tables in this database with pagination support.

Parameters:

namespace_path (Optional[List[str]], default: None ) – The namespace to list tables in. None or empty list represents root namespace.
page_token (Optional[str], default: None ) – Token for pagination. Use the token from a previous response to get the next page of results.
limit (Optional[int], default: None ) – The maximum number of results to return.
Returns:

ListTablesResponse – Response containing table names and optional page_token for pagination.
table_names
¶

table_names(page_token: Optional[str] = None, limit: int = 10, *, namespace_path: Optional[List[str]] = None) -> Iterable[str]
List all tables in this database, in sorted order

Parameters:

namespace_path (Optional[List[str]], default: None ) – The namespace to list tables in. Empty list represents root namespace.
page_token (Optional[str], default: None ) – The token to use for pagination. If not present, start from the beginning. Typically, this token is last table name from the previous page.
limit (int, default: 10 ) – The size of the page to return.
Returns:

Iterable of str –
create_table
¶

create_table(name: str, data: Optional[DATA] = None, schema: Optional[Union[Schema, LanceModel]] = None, mode: str = 'create', exist_ok: bool = False, on_bad_vectors: str = 'error', fill_value: float = 0.0, embedding_functions: Optional[List[EmbeddingFunctionConfig]] = None, *, namespace_path: Optional[List[str]] = None, storage_options: Optional[Dict[str, str]] = None, data_storage_version: Optional[str] = None, enable_v2_manifest_paths: Optional[bool] = None) -> Table
Create a Table in the database.

Parameters:

name (str) – The name of the table.
namespace_path (Optional[List[str]], default: None ) – The namespace to create the table in. Empty list represents root namespace.
data (Optional[DATA], default: None ) – User must provide at least one of data or schema. Acceptable types are:
list-of-dict

pandas.DataFrame

pyarrow.Table or pyarrow.RecordBatch

schema (Optional[Union[Schema, LanceModel]], default: None ) – Acceptable types are:
pyarrow.Schema

LanceModel

mode (str, default: 'create' ) – The mode to use when creating the table. Can be either "create" or "overwrite". By default, if the table already exists, an exception is raised. If you want to overwrite the table, use mode="overwrite".
exist_ok (bool, default: False ) – If a table by the same name already exists, then raise an exception if exist_ok=False. If exist_ok=True, then open the existing table; it will not add the provided data but will validate against any schema that's specified.
on_bad_vectors (str, default: 'error' ) – What to do if any of the vectors are not the same size or contains NaNs. One of "error", "drop", "fill".
fill_value (float, default: 0.0 ) – The value to use when filling vectors. Only used if on_bad_vectors="fill".
storage_options (Optional[Dict[str, str]], default: None ) – Additional options for the storage backend. Options already set on the connection will be inherited by the table, but can be overridden here. See available options at https://docs.lancedb.com/storage/
To enable stable row IDs (row IDs remain stable after compaction, update, delete, and merges), set new_table_enable_stable_row_ids to "true" in storage_options when connecting to the database.

data_storage_version (Optional[str], default: None ) – Deprecated. Set storage_options when connecting to the database and set new_table_data_storage_version in the options.
enable_v2_manifest_paths (Optional[bool], default: None ) – Deprecated. Set storage_options when connecting to the database and set new_table_enable_v2_manifest_paths in the options.
Returns:

LanceTable – A reference to the newly created table.
!!! note – The vector index won't be created by default. To create the index, call the create_index method on the table.
Examples:

Can create with list of tuples or dictionaries:


>>> import lancedb
>>> db = lancedb.connect("./.lancedb")
>>> data = [{"vector": [1.1, 1.2], "lat": 45.5, "long": -122.7},
...         {"vector": [0.2, 1.8], "lat": 40.1, "long":  -74.1}]
>>> db.create_table("my_table", data)
LanceTable(name='my_table', ...)
>>> db["my_table"].head()
pyarrow.Table
vector: fixed_size_list<item: float>[2]
  child 0, item: float
lat: double
long: double
----
vector: [[[1.1,1.2],[0.2,1.8]]]
lat: [[45.5,40.1]]
long: [[-122.7,-74.1]]
You can also pass a pandas DataFrame:


>>> import pandas as pd
>>> data = pd.DataFrame({
...    "vector": [[1.1, 1.2], [0.2, 1.8]],
...    "lat": [45.5, 40.1],
...    "long": [-122.7, -74.1]
... })
>>> db.create_table("table2", data)
LanceTable(name='table2', ...)
>>> db["table2"].head()
pyarrow.Table
vector: fixed_size_list<item: float>[2]
  child 0, item: float
lat: double
long: double
----
vector: [[[1.1,1.2],[0.2,1.8]]]
lat: [[45.5,40.1]]
long: [[-122.7,-74.1]]
Data is converted to Arrow before being written to disk. For maximum control over how data is saved, either provide the PyArrow schema to convert to or else provide a PyArrow Table directly.


>>> import pyarrow as pa
>>> custom_schema = pa.schema([
...   pa.field("vector", pa.list_(pa.float32(), 2)),
...   pa.field("lat", pa.float32()),
...   pa.field("long", pa.float32())
... ])
>>> db.create_table("table3", data, schema = custom_schema)
LanceTable(name='table3', ...)
>>> db["table3"].head()
pyarrow.Table
vector: fixed_size_list<item: float>[2]
  child 0, item: float
lat: float
long: float
----
vector: [[[1.1,1.2],[0.2,1.8]]]
lat: [[45.5,40.1]]
long: [[-122.7,-74.1]]
It is also possible to create an table from [Iterable[pa.RecordBatch]]:


>>> import pyarrow as pa
>>> def make_batches():
...     for i in range(5):
...         yield pa.RecordBatch.from_arrays(
...             [
...                 pa.array([[3.1, 4.1], [5.9, 26.5]],
...                     pa.list_(pa.float32(), 2)),
...                 pa.array(["foo", "bar"]),
...                 pa.array([10.0, 20.0]),
...             ],
...             ["vector", "item", "price"],
...         )
>>> schema=pa.schema([
...     pa.field("vector", pa.list_(pa.float32(), 2)),
...     pa.field("item", pa.utf8()),
...     pa.field("price", pa.float32()),
... ])
>>> db.create_table("table4", make_batches(), schema=schema)
LanceTable(name='table4', ...)
open_table
¶

open_table(name: str, *, namespace_path: Optional[List[str]] = None, storage_options: Optional[Dict[str, str]] = None, index_cache_size: Optional[int] = None, branch: Optional[str] = None, version: Optional[int] = None) -> Table
Open a Lance Table in the database.

Parameters:

name (str) – The name of the table.
namespace_path (Optional[List[str]], default: None ) – The namespace to open the table from. None or empty list represents root namespace.
index_cache_size (Optional[int], default: None ) – Deprecated: Use session-level cache configuration instead. Create a Session with custom cache sizes and pass it to lancedb.connect().
Set the size of the index cache, specified as a number of entries

The exact meaning of an "entry" will depend on the type of index: * IVF - there is one entry for each IVF partition * BTREE - there is one entry for the entire index

This cache applies to the entire opened table, across all indices. Setting this value higher will increase performance on larger datasets at the expense of more RAM

storage_options (Optional[Dict[str, str]], default: None ) – Additional options for the storage backend. Options already set on the connection will be inherited by the table, but can be overridden here. See available options at https://docs.lancedb.com/storage/
branch (Optional[str], default: None ) – If provided, open a handle scoped to this branch instead of the default branch. Reads and writes operate in the branch's context.
version (Optional[int], default: None ) – If provided, open the table pinned to this version, producing a read-only handle. Composes with branch: when both are given, opens that branch at the version; otherwise opens main at the version. Call checkout_latest to return to a writable state.
Returns:

A LanceTable object representing the table. –
drop_table
¶

drop_table(name: str, namespace_path: Optional[List[str]] = None)
Drop a table from the database.

Parameters:

name (str) – The name of the table.
namespace_path (Optional[List[str]], default: None ) – The namespace to drop the table from. Empty list represents root namespace.
rename_table
¶

rename_table(cur_name: str, new_name: str, cur_namespace_path: Optional[List[str]] = None, new_namespace_path: Optional[List[str]] = None)
Rename a table in the database.

Parameters:

cur_name (str) – The current name of the table.
new_name (str) – The new name of the table.
cur_namespace_path (Optional[List[str]], default: None ) – The namespace of the current table. None or empty list represents root namespace.
new_namespace_path (Optional[List[str]], default: None ) – The namespace to move the table to. If not specified, defaults to the same as cur_namespace.
drop_database
¶

drop_database()
Drop database This is the same thing as dropping all the tables

drop_all_tables
¶

drop_all_tables(namespace_path: Optional[List[str]] = None)
Drop all tables from the database

Parameters:

namespace_path (Optional[List[str]], default: None ) – The namespace to drop all tables from. None or empty list represents root namespace.
namespace_client
¶

namespace_client() -> LanceNamespace
Get the equivalent namespace client for this connection.

For native storage connections, this returns a DirectoryNamespace pointing to the same root with the same storage options.

For namespace connections, this returns the backing namespace client.

For enterprise (remote) connections, this returns a RestNamespace with the same URI and authentication headers.

Returns:

LanceNamespace – The namespace client for this connection.
serialize
¶

serialize() -> str
Serialize this connection for reconstruction.

The returned string can be passed to :func:lancedb.deserialize_conn to recreate an equivalent connection, e.g. in a remote worker.

Returns:

str – Serialized representation of this connection.
Tables (Synchronous)
¶
lancedb.table.Table
¶
Bases: ABC

A Table is a collection of Records in a LanceDB Database.

Examples:

Create using DBConnection.create_table (more examples in that method's documentation).


>>> import lancedb
>>> db = lancedb.connect("./.lancedb")
>>> table = db.create_table("my_table", data=[{"vector": [1.1, 1.2], "b": 2}])
>>> table.head()
pyarrow.Table
vector: fixed_size_list<item: float>[2]
  child 0, item: float
b: int64
----
vector: [[[1.1,1.2]]]
b: [[2]]
Can append new data with Table.add().


>>> table.add([{"vector": [0.5, 1.3], "b": 4}])
AddResult(version=2)
Can query the table with Table.search.


>>> table.search([0.4, 0.4]).select(["b", "vector"]).to_pandas()
   b      vector  _distance
0  4  [0.5, 1.3]       0.82
1  2  [1.1, 1.2]       1.13
Search queries are much faster when an index is created. See Table.create_index.

name
¶

name: str
The name of this Table

version
¶

version: int
The version of this Table

schema
¶

schema: Schema
The Arrow Schema of this Table

tags
¶

tags: Tags
Tag management for the table.

Similar to Git, tags are a way to add metadata to a specific version of the table.

.. warning::


Tagged versions are exempted from the :py:meth:`cleanup_old_versions()`
process.

To remove a version that has been tagged, you must first
:py:meth:`~Tags.delete` the associated tag.
Examples:

.. code-block:: python


table = db.open_table("my_table")
table.tags.create("v2-prod-20250203", 10)

tags = table.tags.list()
branches
¶

branches: 'Branches'
Branch management for the table.

Branches are isolated, writable lines of history forked from another branch (or version). Writes on a branch do not affect main.

embedding_functions
¶

embedding_functions: Dict[str, EmbeddingFunctionConfig]
Get a mapping from vector column name to it's configured embedding function.

current_branch
¶

current_branch() -> Optional[str]
The branch this table handle is scoped to, or None for main.

count_rows
¶

count_rows(filter: Optional[str] = None) -> int
Count the number of rows in the table.

Parameters:

filter (Optional[str], default: None ) – A SQL where clause to filter the rows to count.
to_pandas
¶

to_pandas(blob_mode: BlobMode = 'lazy', **kwargs) -> 'pandas.DataFrame'
Return the table as a pandas DataFrame.

Parameters:

blob_mode (BlobMode, default: 'lazy' ) – Controls how blob columns are returned for backends that support Lance blob-aware pandas conversion.
**kwargs – Forwarded to PyArrow / Lance pandas conversion.
Returns:

DataFrame –
to_arrow
¶

to_arrow() -> Table
Return the table as a pyarrow Table.

Returns:

Table –
to_lance
¶

to_lance(**kwargs) -> LanceDataset
Return the table as a lance.LanceDataset.

Returns:

LanceDataset –
to_polars
¶

to_polars(**kwargs) -> 'pl.DataFrame'
Return the table as a polars.DataFrame.

Returns:

DataFrame –
create_index
¶

create_index(metric: DistanceType = 'l2', num_partitions: Optional[int] = None, num_sub_vectors: Optional[int] = None, vector_column_name: str = VECTOR_COLUMN_NAME, replace: bool = True, accelerator: Optional[str] = None, index_cache_size: Optional[int] = None, *, index_type: VectorIndexType = 'IVF_PQ', wait_timeout: Optional[timedelta] = None, num_bits: int = 8, max_iterations: int = 50, sample_rate: int = 256, m: int = 20, ef_construction: int = 300, config: Optional[IndexConfigType] = None, name: Optional[str] = None, train: bool = True, target_partition_size: Optional[int] = None)
Create an index on a column.

This method supports both the new unified API and the legacy API for backwards compatibility. The new API takes the column name as the first positional argument and an index configuration object via config; the legacy API takes the distance metric as the first argument plus separate vector_column_name / num_partitions / etc. parameters, and emits a DeprecationWarning.

Parameters:

metric (str, default: 'l2' ) – For new API: the column name to index. For legacy API: the distance metric ("l2", "cosine", "dot", "hamming").
config (IndexConfigType, default: None ) – The index configuration object. If provided, uses the new unified API. Can be one of: IvfFlat, IvfPq, IvfSq, IvfRq, HnswPq, HnswSq, BTree, Bitmap, LabelList, Fm, FTS.
replace (bool, default: True ) – Whether to replace an existing index on this column.
wait_timeout (timedelta, default: None ) – Timeout to wait for async indexing to complete.
name (str, default: None ) – Custom name for the index.
train (bool, default: True ) – Whether to train the index with existing data.
Examples:

New API (recommended):


>>> table.create_index(
...     "vector", config=IvfPq(distance_type="l2")
... )
>>> table.create_index("category", config=BTree())
>>> table.create_index("content", config=FTS())
Legacy API (deprecated):


>>> table.create_index(
...     "l2", vector_column_name="vector"
... )
drop_index
¶

drop_index(name: str) -> None
Drop an index from the table.

Parameters:

name (str) – The name of the index to drop.
Notes
This does not delete the index from disk, it just removes it from the table. To delete the index, run optimize after dropping the index.

Use list_indices to find the names of the indices.

wait_for_index
¶

wait_for_index(index_names: Iterable[str], timeout: timedelta = timedelta(seconds=300)) -> None
Wait for indexing to complete for the given index names. This will poll the table until all the indices are fully indexed, or raise a timeout exception if the timeout is reached.

Parameters:

index_names (Iterable[str]) – The name of the indices to poll
timeout (timedelta, default: timedelta(seconds=300) ) – Timeout to wait for asynchronous indexing. The default is 5 minutes.
stats
¶

stats() -> TableStatistics
Retrieve table and fragment statistics.

create_scalar_index
¶

create_scalar_index(column: str, *, replace: bool = True, index_type: ScalarIndexType = 'BTREE', wait_timeout: Optional[timedelta] = None, name: Optional[str] = None)
Create a scalar index on a column.

Parameters:

column (str) – The column to be indexed. Must be a boolean, integer, float, or string column.
replace (bool, default: True ) – Replace the existing index if it exists.
index_type (ScalarIndexType, default: 'BTREE' ) – The type of index to create.
wait_timeout (Optional[timedelta], default: None ) – The timeout to wait if indexing is asynchronous.
name (Optional[str], default: None ) – The name of the index. If not provided, a default name will be generated.
Examples:

Scalar indices, like vector indices, can be used to speed up scans. A scalar index can speed up scans that contain filter expressions on the indexed column. For example, the following scan will be faster if the column my_col has a scalar index:


>>> import lancedb
>>> db = lancedb.connect("/data/lance")
>>> img_table = db.open_table("images")
>>> my_df = img_table.search().where("my_col = 7",
...                                  prefilter=True).to_pandas()
Scalar indices can also speed up scans containing a vector search and a prefilter:


>>> import lancedb
>>> db = lancedb.connect("/data/lance")
>>> img_table = db.open_table("images")
>>> img_table.search([1, 2, 3, 4], vector_column_name="vector")
...     .where("my_col != 7", prefilter=True)
...     .to_pandas()
Scalar indices can only speed up scans for basic filters using equality, comparison, range (e.g. my_col BETWEEN 0 AND 100), and set membership (e.g. my_col IN (0, 1, 2))

Scalar indices can be used if the filter contains multiple indexed columns and the filter criteria are AND'd or OR'd together (e.g. my_col < 0 AND other_col> 100)

Scalar indices may be used if the filter contains non-indexed columns but, depending on the structure of the filter, they may not be usable. For example, if the column not_indexed does not have a scalar index then the filter my_col = 0 OR not_indexed = 1 will not be able to use any scalar index on my_col.

create_fts_index
¶

create_fts_index(field_names: Union[str, List[str]], *, ordering_field_names: Optional[Union[str, List[str]]] = None, replace: bool = False, writer_heap_size: Optional[int] = 1024 * 1024 * 1024, use_tantivy: bool = False, tokenizer_name: Optional[str] = None, with_position: bool = False, base_tokenizer: BaseTokenizerType = 'simple', language: str = 'English', max_token_length: Optional[int] = 40, lower_case: bool = True, stem: bool = True, remove_stop_words: bool = True, ascii_folding: bool = True, ngram_min_length: int = 3, ngram_max_length: int = 3, prefix_only: bool = False, wait_timeout: Optional[timedelta] = None, name: Optional[str] = None)
Create a full-text search index on the table.

Warning - this API is highly experimental and is highly likely to change in the future.

Parameters:

field_names (Union[str, List[str]]) – The name of the field to index. Native FTS indexes can only be created on a single field at a time. To search over multiple text fields, create a separate FTS index for each field.
replace (bool, default: False ) – If True, replace the existing index if it exists. Note that this is not yet an atomic operation; the index will be temporarily unavailable while the new index is being created.
writer_heap_size (Optional[int], default: 1024 * 1024 * 1024 ) – Deprecated legacy Tantivy parameter. Any value other than the default raises an error.
ordering_field_names (Optional[Union[str, List[str]]], default: None ) – Deprecated legacy Tantivy parameter. Setting this raises an error.
tokenizer_name (Optional[str], default: None ) – A compatibility alias for native tokenizer configs. Can be "raw", "default" or the 2 letter language code followed by "_stem". So for english it would be "en_stem". For new native FTS indexes, use base_tokenizer directly; tokenizer_name is a legacy compatibility alias and does not expose model-backed tokenizer names such as jieba/default or lindera/ipadic.
use_tantivy (bool, default: False ) – Deprecated legacy Tantivy parameter. Setting this to True raises an error.
with_position (bool, default: False ) – If False, do not store the positions of the terms in the text. This can reduce the size of the index and improve indexing speed. But it will raise an exception for phrase queries.
base_tokenizer (str, default: "simple" ) – The base tokenizer to use for tokenization. Options are: - "simple": Splits text by whitespace and punctuation. - "whitespace": Split text by whitespace, but not punctuation. - "raw": No tokenization. The entire text is treated as a single token. - "ngram": N-Gram tokenizer. - "jieba/": Jieba tokenizer loaded from Lance's language model home. - "lindera/": Lindera tokenizer loaded from Lance's language model home.
language (str, default: "English" ) – The language to use for stemming and stop-word removal. This is not the primary way to enable CJK tokenization.
max_token_length (int, default: 40 ) – The maximum token length to index. Tokens longer than this length will be ignored.
lower_case (bool, default: True ) – Whether to convert the token to lower case. This makes queries case-insensitive.
stem (bool, default: True ) – Whether to stem the token. Stemming reduces words to their root form. For example, in English "running" and "runs" would both be reduced to "run".
remove_stop_words (bool, default: True ) – Whether to remove stop words. Stop words are common words that are often removed from text before indexing. For example, in English "the" and "and".
ascii_folding (bool, default: True ) – Whether to fold ASCII characters. This converts accented characters to their ASCII equivalent. For example, "café" would be converted to "cafe".
ngram_min_length (int, default: 3 ) – The minimum length of an n-gram.
ngram_max_length (int, default: 3 ) – The maximum length of an n-gram.
prefix_only (bool, default: False ) – Whether to only index the prefix of the token for ngram tokenizer.
wait_timeout (Optional[timedelta], default: None ) – The timeout to wait if indexing is asynchronous.
name (Optional[str], default: None ) – The name of the index. If not provided, a default name will be generated.
Notes
Model-backed tokenizers such as jieba/default and lindera/ipadic require tokenizer models in Lance's language model home. Set LANCE_LANGUAGE_MODEL_HOME to override the default platform data directory under lance/language_models.

add
¶

add(data: DATA, mode: AddMode = 'append', on_bad_vectors: OnBadVectorsType = 'error', fill_value: float = 0.0, progress: Optional[Union[bool, Callable, Any]] = None) -> AddResult
Add more data to the Table.

Parameters:

data (DATA) – The data to insert into the table. Acceptable types are:
list-of-dict

pandas.DataFrame

pyarrow.Table or pyarrow.RecordBatch

mode (AddMode, default: 'append' ) – The mode to use when writing the data. Valid values are "append" and "overwrite".
on_bad_vectors (OnBadVectorsType, default: 'error' ) – What to do if any of the vectors are not the same size or contains NaNs. One of "error", "drop", "fill".
fill_value (float, default: 0.0 ) – The value to use when filling vectors. Only used if on_bad_vectors="fill".
progress (Optional[Union[bool, Callable, Any]], default: None ) – Progress reporting during the add operation. Can be:
True to automatically create and display a tqdm progress bar (requires tqdm to be installed)::

table.add(data, progress=True)

A callable that receives a dict with keys output_rows, output_bytes, total_rows, elapsed_seconds, active_tasks, total_tasks, and done::

def on_progress(p): print(f"{p['output_rows']}/{p['total_rows']} rows, " f"{p['active_tasks']}/{p['total_tasks']} workers") table.add(data, progress=on_progress)

A tqdm-compatible progress bar whose total and update() will be called automatically. The postfix shows write throughput (MB/s) and active worker count::

with tqdm() as pbar: table.add(data, progress=pbar)

Returns:

AddResult – An object containing the new version number of the table after adding data.
merge_insert
¶

merge_insert(on: Union[str, Iterable[str]]) -> LanceMergeInsertBuilder
Returns a LanceMergeInsertBuilder that can be used to create a "merge insert" operation

This operation can add rows, update rows, and remove rows all in a single transaction. It is a very generic tool that can be used to create behaviors like "insert if not exists", "update or insert (i.e. upsert)", or even replace a portion of existing data with new data (e.g. replace all data where month="january")

The merge insert operation works by combining new data from a source table with existing data in a target table by using a join. There are three categories of records.

"Matched" records are records that exist in both the source table and the target table. "Not matched" records exist only in the source table (e.g. these are new data) "Not matched by source" records exist only in the target table (this is old data)

The builder returned by this method can be used to customize what should happen for each category of data.

Please note that the data may appear to be reordered as part of this operation. This is because updated rows will be deleted from the dataset and then reinserted at the end with the new values.

Parameters:

on (Union[str, Iterable[str]]) – A column (or columns) to join on. This is how records from the source table and target table are matched. Typically this is some kind of key or id column.
Examples:


>>> import lancedb
>>> data = pa.table({"a": [2, 1, 3], "b": ["a", "b", "c"]})
>>> db = lancedb.connect("./.lancedb")
>>> table = db.create_table("my_table", data)
>>> new_data = pa.table({"a": [2, 3, 4], "b": ["x", "y", "z"]})
>>> # Perform a "upsert" operation
>>> res = table.merge_insert("a")     \
...      .when_matched_update_all()     \
...      .when_not_matched_insert_all() \
...      .execute(new_data)
>>> res
MergeResult(version=2, num_updated_rows=2, num_inserted_rows=1, num_deleted_rows=0, num_attempts=1, num_rows=3)
>>> # The order of new rows is non-deterministic since we use
>>> # a hash-join as part of this operation and so we sort here
>>> table.to_arrow().sort_by("a").to_pandas()
   a  b
0  1  b
1  2  x
2  3  y
3  4  z
search
¶

search(query: Optional[Union[VEC, str, 'PIL.Image.Image', Tuple, FullTextQuery]] = None, vector_column_name: Optional[str] = None, query_type: QueryType = 'auto', ordering_field_name: Optional[str] = None, fts_columns: Optional[Union[str, List[str]]] = None) -> LanceQueryBuilder
Create a search query to find the nearest neighbors of the given query vector. We currently support vector search and [full-text search][experimental-full-text-search].

All query options are defined in LanceQueryBuilder.

Examples:


>>> import lancedb
>>> db = lancedb.connect("./.lancedb")
>>> data = [
...    {"original_width": 100, "caption": "bar", "vector": [0.1, 2.3, 4.5]},
...    {"original_width": 2000, "caption": "foo",  "vector": [0.5, 3.4, 1.3]},
...    {"original_width": 3000, "caption": "test", "vector": [0.3, 6.2, 2.6]}
... ]
>>> table = db.create_table("my_table", data)
>>> query = [0.4, 1.4, 2.4]
>>> (table.search(query)
...     .where("original_width > 1000", prefilter=True)
...     .select(["caption", "original_width", "vector"])
...     .limit(2)
...     .to_pandas())
  caption  original_width           vector  _distance
0     foo            2000  [0.5, 3.4, 1.3]   5.220000
1    test            3000  [0.3, 6.2, 2.6]  23.089996
Parameters:

query (Optional[Union[VEC, str, 'PIL.Image.Image', Tuple, FullTextQuery]], default: None ) – The targetted vector to search for.
default None. Acceptable types are: list, np.ndarray, PIL.Image.Image

If None then the select/where/limit clauses are applied to filter the table

vector_column_name (Optional[str], default: None ) – The name of the vector column to search.
The vector column needs to be a pyarrow fixed size list type

If not specified then the vector column is inferred from the table schema

If the table has multiple vector columns then the vector_column_name needs to be specified. Otherwise, an error is raised.

query_type (QueryType, default: 'auto' ) – default "auto". Acceptable types are: "vector", "fts", "hybrid", or "auto"
If "auto" then the query type is inferred from the query;

If query is a list/np.ndarray then the query type is "vector";

If query is a PIL.Image.Image then either do vector search, or raise an error if no corresponding embedding function is found.

If query is a string, then the query type is "vector" if the table has embedding functions else the query type is "fts"

Returns:

LanceQueryBuilder – A query builder object representing the query. Once executed, the query returns
selected columns

the vector

and also the "_distance" column which is the distance between the query vector and the returned vector.

take_offsets
¶

take_offsets(offsets: list[int], *, with_row_id: bool = False) -> LanceTakeQueryBuilder
Take a list of offsets from the table.

Offsets are 0-indexed and relative to the current version of the table. Offsets are not stable. A row with an offset of N may have a different offset in a different version of the table (e.g. if an earlier row is deleted).

Offsets are mostly useful for sampling as the set of all valid offsets is easily known in advance to be [0, len(table)).

No guarantees are made regarding the order in which results are returned. If you desire an output order that matches the order of the given offsets, you will need to add the row offset column to the output and align it yourself.

Parameters:

offsets (list[int]) – The offsets to take.
Returns:

RecordBatch – A record batch containing the rows at the given offsets.
take_row_ids
¶

take_row_ids(row_ids: list[int], *, with_row_id: bool = False) -> LanceTakeQueryBuilder
Take a list of row ids from the table.

Row ids are not stable and are relative to the current version of the table. They can change due to compaction and updates.

No guarantees are made regarding the order in which results are returned. If you desire an output order that matches the order of the given ids, you will need to add the row id column to the output and align it yourself.

Unlike offsets, row ids are not 0-indexed and no assumptions should be made about the possible range of row ids. In order to use this method you must first obtain the row ids by scanning or searching the table.

Even so, row ids are more stable than offsets and can be useful in some situations.

There is an ongoing effort to make row ids stable which is tracked at https://github.com/lancedb/lancedb/issues/1120

Parameters:

row_ids (list[int]) – The row ids to take.
Returns:

AsyncTakeQuery – A query object that can be executed to get the rows.
delete
¶

delete(where: Union[str, Expr]) -> DeleteResult
Delete rows from the table.

This can be used to delete a single row, many rows, all rows, or sometimes no rows (if your predicate matches nothing).

Parameters:

where (Union[str, Expr]) – The filter condition. Can be a SQL string or a type-safe :class:~lancedb.expr.Expr built with :func:~lancedb.expr.col and :func:~lancedb.expr.lit.
The filter must not be empty, or it will error.

Returns:

DeleteResult – An object containing the new version number of the table after deletion.
Examples:


>>> import lancedb
>>> data = [
...    {"x": 1, "vector": [1.0, 2]},
...    {"x": 2, "vector": [3.0, 4]},
...    {"x": 3, "vector": [5.0, 6]}
... ]
>>> db = lancedb.connect("./.lancedb")
>>> table = db.create_table("my_table", data)
>>> table.to_pandas()
   x      vector
0  1  [1.0, 2.0]
1  2  [3.0, 4.0]
2  3  [5.0, 6.0]
>>> table.delete("x = 2")
DeleteResult(num_deleted_rows=1, version=2)
>>> table.to_pandas()
   x      vector
0  1  [1.0, 2.0]
1  3  [5.0, 6.0]
If you have a list of values to delete, you can combine them into a stringified list and use the IN operator:


>>> to_remove = [1, 5]
>>> to_remove = ", ".join([str(v) for v in to_remove])
>>> to_remove
'1, 5'
>>> table.delete(f"x IN ({to_remove})")
DeleteResult(num_deleted_rows=1, version=3)
>>> table.to_pandas()
   x      vector
0  3  [5.0, 6.0]
update
¶

update(where: Optional[str] = None, values: Optional[dict] = None, *, values_sql: Optional[Dict[str, str]] = None) -> UpdateResult
This can be used to update zero to all rows depending on how many rows match the where clause. If no where clause is provided, then all rows will be updated.

Either values or values_sql must be provided. You cannot provide both.

Parameters:

where (Optional[str], default: None ) – The SQL where clause to use when updating rows. For example, 'x = 2' or 'x IN (1, 2, 3)'. The filter must not be empty, or it will error.
values (Optional[dict], default: None ) – The values to update. The keys are the column names and the values are the values to set.
values_sql (Optional[Dict[str, str]], default: None ) – The values to update, expressed as SQL expression strings. These can reference existing columns. For example, {"x": "x + 1"} will increment the x column by 1.
Returns:

UpdateResult –
rows_updated: The number of rows that were updated
version: The new version number of the table after the update
Examples:


>>> import lancedb
>>> import pandas as pd
>>> data = pd.DataFrame({"x": [1, 2, 3], "vector": [[1.0, 2], [3, 4], [5, 6]]})
>>> db = lancedb.connect("./.lancedb")
>>> table = db.create_table("my_table", data)
>>> table.to_pandas()
   x      vector
0  1  [1.0, 2.0]
1  2  [3.0, 4.0]
2  3  [5.0, 6.0]
>>> table.update(where="x = 2", values={"vector": [10.0, 10]})
UpdateResult(rows_updated=1, version=2)
>>> table.to_pandas()
   x        vector
0  1    [1.0, 2.0]
1  3    [5.0, 6.0]
2  2  [10.0, 10.0]
>>> table.update(values_sql={"x": "x + 1"})
UpdateResult(rows_updated=3, version=3)
>>> table.to_pandas()
   x        vector
0  2    [1.0, 2.0]
1  4    [5.0, 6.0]
2  3  [10.0, 10.0]
cleanup_old_versions
¶

cleanup_old_versions(older_than: Optional[timedelta] = None, *, delete_unverified: bool = False) -> 'CleanupStats'
Clean up old versions of the table, freeing disk space.

Parameters:

older_than (Optional[timedelta], default: None ) – The minimum age of the version to delete. If None, then this defaults to two weeks.
delete_unverified (bool, default: False ) – Because they may be part of an in-progress transaction, files newer than 7 days old are not deleted by default. If you are sure that there are no in-progress transactions, then you can set this to True to delete all files older than older_than.
Returns:

CleanupStats – The stats of the cleanup operation, including how many bytes were freed.
See Also
Table.optimize: A more comprehensive optimization operation that includes cleanup as well as other operations.

Notes
This function is not available in LanceDb Cloud (since LanceDB Cloud manages cleanup for you automatically)

compact_files
¶

compact_files(*args, **kwargs)
Run the compaction process on the table. This can be run after making several small appends to optimize the table for faster reads.

Arguments are passed onto Lance's [compact_files][lance.dataset.DatasetOptimizer.compact_files]. For most cases, the default should be fine.

See Also
Table.optimize: A more comprehensive optimization operation that includes cleanup as well as other operations.

Notes
This function is not available in LanceDB Cloud (since LanceDB Cloud manages compaction for you automatically)

optimize
¶

optimize(*, cleanup_older_than: Optional[timedelta] = None, delete_unverified: bool = False, retrain: bool = False)
Optimize the on-disk data and indices for better performance.

Modeled after VACUUM in PostgreSQL.

Optimization covers three operations:

Compaction: Merges small files into larger ones
Prune: Removes old versions of the dataset
Index: Optimizes the indices, adding new data to existing indices
Parameters:

cleanup_older_than (Optional[timedelta], default: None ) – All files belonging to versions older than this will be removed. Set to 0 days to remove all versions except the latest. The latest version is never removed.
delete_unverified (bool, default: False ) – Files leftover from a failed transaction may appear to be part of an in-progress operation (e.g. appending new data) and these files will not be deleted unless they are at least 7 days old. If delete_unverified is True then these files will be deleted regardless of their age.
.. warning::


This should only be set to True if you can guarantee that no other
process is currently working on this dataset. Otherwise the dataset
could be put into a corrupted state.
retrain (bool, default: False ) – This parameter is no longer used and is deprecated.
The –
data –
optimize –
you –
modification –
list_indices
¶

list_indices() -> Iterable[IndexConfig]
List all indices that have been created with Table.create_index

index_stats
¶

index_stats(index_name: str) -> Optional[IndexStatistics]
Retrieve statistics about an index

Parameters:

index_name (str) – The name of the index to retrieve statistics for
Returns:

IndexStatistics or None – The statistics about the index. Returns None if the index does not exist.
add_columns
¶

add_columns(transforms: Dict[str, str] | Field | List[Field] | Schema)
Add new columns with defined values.

Parameters:

transforms (Dict[str, str] | Field | List[Field] | Schema) – A map of column name to a SQL expression to use to calculate the value of the new column. These expressions will be evaluated for each row in the table, and can reference existing columns. Alternatively, a pyarrow Field or Schema can be provided to add new columns with the specified data types. The new columns will be initialized with null values.
Returns:

AddColumnsResult – version: the new version number of the table after adding columns.
alter_columns
¶

alter_columns(*alterations: Iterable[Dict[str, str]])
Alter column names and nullability.

Parameters:

alterations (Iterable[Dict[str, Any]], default: () ) – A sequence of dictionaries, each with the following keys: - "path": str The column path to alter. For a top-level column, this is the name. For a nested column, this is the dot-separated path, e.g. "a.b.c". - "rename": str, optional The new name of the column. If not specified, the column name is not changed. - "data_type": pyarrow.DataType, optional The new data type of the column. Existing values will be casted to this type. If not specified, the column data type is not changed. - "nullable": bool, optional Whether the column should be nullable. If not specified, the column nullability is not changed. Only non-nullable columns can be changed to nullable. Currently, you cannot change a nullable column to non-nullable.
Returns:

AlterColumnsResult – version: the new version number of the table after the alteration.
update_field_metadata
¶

update_field_metadata(*updates: dict[str, Any]) -> UpdateFieldMetadataResult
Update per-field (column) metadata.

Parameters:

updates (dict, default: () ) – One or more dicts, each with: - "path": str — dot-path to the field (e.g. "embedding" or "a.b.c"). - "metadata": dict[str, str | None] — keys to set; a value of None deletes that key. - "replace": bool, optional — replace the field's whole metadata map instead of merging (default False).
Returns:

UpdateFieldMetadataResult – version: the new table version after the update.
drop_columns
¶

drop_columns(columns: Iterable[str]) -> DropColumnsResult
Drop columns from the table.

Parameters:

columns (Iterable[str]) – The names of the columns to drop.
Returns:

DropColumnsResult – version: the new version number of the table dropping the columns.
checkout
¶

checkout(version: Union[int, str])
Checks out a specific version of the Table

Any read operation on the table will now access the data at the checked out version. As a consequence, calling this method will disable any read consistency interval that was previously set.

This is a read-only operation that turns the table into a sort of "view" or "detached head". Other table instances will not be affected. To make the change permanent you can use the [Self::restore] method.

Any operation that modifies the table will fail while the table is in a checked out state.

Parameters:

version (Union[int, str]) – The version to check out. A version number (int) or a tag (str) can be provided.
To –
checkout_latest
¶

checkout_latest()
Ensures the table is pointing at the latest version

This can be used to manually update a table when the read_consistency_interval is None It can also be used to undo a [Self::checkout] operation

restore
¶

restore(version: Optional[Union[int, str]] = None)
Restore a version of the table. This is an in-place operation.

This creates a new version where the data is equivalent to the specified previous version. Data is not copied (as of python-v0.2.1).

Parameters:

version (int or str, default: None ) – The version number or version tag to restore. If unspecified then restores the currently checked out version. If the currently checked out version is the latest version then this is a no-op.
list_versions
¶

list_versions() -> List[Dict[str, Any]]
List all versions of the table

uses_v2_manifest_paths
¶

uses_v2_manifest_paths() -> bool
Check if the table is using the new v2 manifest paths.

Returns:

bool – True if the table is using the new v2 manifest paths, False otherwise.
migrate_v2_manifest_paths
¶

migrate_v2_manifest_paths()
Migrate the manifest paths to the new format.

This will update the manifest to use the new v2 format for paths.

This function is idempotent, and can be run multiple times without changing the state of the object store.

Danger

This should not be run while other concurrent operations are happening. And it should also run until completion before resuming other operations.

You can use Table.uses_v2_manifest_paths to check if the table is already using the new path style.

lancedb.table.FragmentStatistics
¶
Statistics about fragments.

num_fragments
¶

num_fragments: int
num_small_fragments
¶

num_small_fragments: int
lengths
¶

lengths: FragmentSummaryStats
lancedb.table.FragmentSummaryStats
¶
Statistics about fragments sizes

min
¶

min: int
max
¶

max: int
mean
¶

mean: int
p25
¶

p25: int
p50
¶

p50: int
p75
¶

p75: int
p99
¶

p99: int
lancedb.table.Tags
¶
Table tag manager.

list
¶

list() -> Dict[str, Tag]
List all table tags.

Returns:

dict[str, Tag] – A dictionary mapping tag names to version numbers.
get_version
¶

get_version(tag: str) -> int
Get the version of a tag.

Parameters:

tag (str) – The name of the tag to get the version for.
create
¶

create(tag: str, version: int) -> None
Create a tag for a given table version.

Parameters:

tag (str) – The name of the tag to create. This name must be unique among all tag names for the table.
version (int) – The table version to tag.
delete
¶

delete(tag: str) -> None
Delete tag from the table.

Parameters:

tag (str) – The name of the tag to delete.
update
¶

update(tag: str, version: int) -> None
Update tag to a new version.

Parameters:

tag (str) – The name of the tag to update.
version (int) – The new table version to tag.
Expressions
¶
Type-safe expression builder for filters and projections. Use these instead of raw SQL strings with where and select.

lancedb.expr.Expr
¶
A type-safe expression node.

Construct instances with :func:col and :func:lit, then combine them using Python operators or the named methods below.

Examples:


>>> from lancedb.expr import col, lit
>>> filt = (col("age") > lit(18)) & (col("name").lower() == lit("alice"))
>>> proj = {"double": col("x") * lit(2)}
lower
¶

lower() -> 'Expr'
Convert string column values to lowercase.

upper
¶

upper() -> 'Expr'
Convert string column values to uppercase.

contains
¶

contains(substr: 'ExprLike') -> 'Expr'
Return True where the string contains substr.

isin
¶

isin(values: 'Iterable[ExprLike]') -> 'Expr'
Return True where the value is one of values (SQL IN).

cast
¶

cast(data_type: Union[str, 'pa.DataType']) -> 'Expr'
Cast values to data_type.

Parameters:

data_type (Union[str, 'pa.DataType']) – A PyArrow DataType (e.g. pa.int32()) or one of the type name strings: "bool", "int8", "int16", "int32", "int64", "uint8"–"uint64", "float32", "float64", "string", "date32", "date64".
eq
¶

eq(other: ExprLike) -> 'Expr'
Equal to.

ne
¶

ne(other: ExprLike) -> 'Expr'
Not equal to.

lt
¶

lt(other: ExprLike) -> 'Expr'
Less than.

lte
¶

lte(other: ExprLike) -> 'Expr'
Less than or equal to.

gt
¶

gt(other: ExprLike) -> 'Expr'
Greater than.

gte
¶

gte(other: ExprLike) -> 'Expr'
Greater than or equal to.

and_
¶

and_(other: 'Expr') -> 'Expr'
Logical AND.

or_
¶

or_(other: 'Expr') -> 'Expr'
Logical OR.

to_sql
¶

to_sql() -> str
Render the expression as a SQL string (useful for debugging).

lancedb.expr.col
¶

col(name: str) -> Expr
Reference a table column by name.

Parameters:

name (str) – The column name.
Examples:


>>> from lancedb.expr import col, lit
>>> col("age") > lit(18)
Expr((age > 18))
lancedb.expr.lit
¶

lit(value: Union[bool, int, float, str, bytes]) -> Expr
Create a literal (constant) value expression.

Parameters:

value (Union[bool, int, float, str, bytes]) – A Python bool, int, float, str, or bytes.
Examples:


>>> from lancedb.expr import col, lit
>>> col("price") * lit(1.1)
Expr((price * 1.1))
lancedb.expr.func
¶

func(name: str, *args: ExprLike) -> Expr
Call an arbitrary SQL function by name.

Parameters:

name (str) – The SQL function name (e.g. "lower", "upper").
*args (ExprLike, default: () ) – The function arguments as :class:Expr or plain Python literals.
Examples:


>>> from lancedb.expr import col, func
>>> func("lower", col("name"))
Expr(lower(name))
Querying (Synchronous)
¶
lancedb.query.Query
¶
Bases: BaseModel

A LanceDB Query

Queries are constructed by the Table.search method. This class is a python representation of the query. Normally you will not need to interact with this class directly. You can build up a query and execute it using collection methods such as to_batches(), to_arrow(), to_pandas(), etc.

However, you can use the to_query() method to get the underlying query object. This can be useful for serializing a query or using it in a different context.

vector_column
¶

vector_column: Optional[str] = None
vector
¶

vector: Annotated[Optional[Union[List[float], List[List[float]], Array, List[Array]]], ensure_vector_query] = None
filter
¶

filter: Optional[Union[str, Expr]] = None
postfilter
¶

postfilter: Optional[bool] = None
full_text_query
¶

full_text_query: Optional[FullTextSearchQuery] = None
limit
¶

limit: Optional[int] = None
distance_type
¶

distance_type: Optional[str] = None
columns
¶

columns: Optional[Union[List[str], Dict[str, Union[str, Expr]]]] = None
minimum_nprobes
¶

minimum_nprobes: Optional[int] = None
maximum_nprobes
¶

maximum_nprobes: Optional[int] = None
lower_bound
¶

lower_bound: Optional[float] = None
upper_bound
¶

upper_bound: Optional[float] = None
refine_factor
¶

refine_factor: Optional[int] = None
with_row_id
¶

with_row_id: Optional[bool] = None
with_row_address
¶

with_row_address: Optional[bool] = None
fragments
¶

fragments: Optional[Any] = None
fragment_ids
¶

fragment_ids: Optional[List[int]] = None
offset
¶

offset: Optional[int] = None
fast_search
¶

fast_search: Optional[bool] = None
ef
¶

ef: Optional[int] = None
bypass_vector_index
¶

bypass_vector_index: Optional[bool] = None
order_by
¶

order_by: Optional[List[ColumnOrdering]] = None
model_config
¶

model_config = {'arbitrary_types_allowed': True}
Config
¶
arbitrary_types_allowed
¶

arbitrary_types_allowed = True
from_inner
¶

from_inner(req: PyQueryRequest) -> Self
lancedb.query.LanceQueryBuilder
¶
Bases: ABC

An abstract query builder. Subclasses are defined for vector search, full text search, hybrid, and plain SQL filtering.

create
¶

create(table: 'Table', query: Optional[Union[ndarray, str, 'PIL.Image.Image', Tuple]], query_type: str, vector_column_name: str, ordering_field_name: Optional[str] = None, fts_columns: Optional[Union[str, List[str]]] = None, fast_search: bool = None) -> Self
Create a query builder based on the given query and query type.

Parameters:

table ('Table') – The table to query.
query (Optional[Union[ndarray, str, 'PIL.Image.Image', Tuple]]) – The query to use. If None, an empty query builder is returned which performs simple SQL filtering.
query_type (str) – The type of query to perform. One of "vector", "fts", "hybrid", or "auto". If "auto", the query type is inferred based on the query.
vector_column_name (str) – The name of the vector column to use for vector search.
ordering_field_name (Optional[str], default: None ) – .. deprecated:: 0.27.0 Use order_by() method instead.
fts_columns (Optional[Union[str, List[str]]], default: None ) – The columns to search in for full text search.
fast_search (bool, default: None ) – Skip flat search of unindexed data.
to_df
¶

to_df() -> 'pd.DataFrame'
Deprecated alias for to_pandas(). Please use to_pandas() instead.

Execute the query and return the results as a pandas DataFrame. In addition to the selected columns, LanceDB also returns a vector and also the "_distance" column which is the distance between the query vector and the returned vector.

to_pandas
¶

to_pandas(flatten: Optional[Union[int, bool]] = None, *, blob_mode: BlobMode = 'lazy', timeout: Optional[timedelta] = None, **kwargs) -> 'pd.DataFrame'
Execute the query and return the results as a pandas DataFrame. In addition to the selected columns, LanceDB also returns a vector and also the "_distance" column which is the distance between the query vector and the returned vector.

Parameters:

flatten (Optional[Union[int, bool]], default: None ) – If flatten is True, flatten all nested columns. If flatten is an integer, flatten the nested columns up to the specified depth. If unspecified, do not flatten the nested columns.
timeout (Optional[timedelta], default: None ) – The maximum time to wait for the query to complete. If None, wait indefinitely.
blob_mode (BlobMode, default: 'lazy' ) – Controls how blob columns are returned for plain scan queries. Vector, FTS, hybrid, and other non-native query shapes keep the existing Arrow conversion path and only support blob descriptions.
**kwargs – Forwarded to pyarrow.Table.to_pandas after query execution and optional flattening.
to_arrow
¶

to_arrow(*, timeout: Optional[timedelta] = None) -> Table
Execute the query and return the results as an Apache Arrow Table.

In addition to the selected columns, LanceDB also returns a vector and also the "_distance" column which is the distance between the query vector and the returned vectors.

Parameters:

timeout (Optional[timedelta], default: None ) – The maximum time to wait for the query to complete. If None, wait indefinitely.
to_batches
¶

to_batches(batch_size: Optional[int] = None, *, timeout: Optional[timedelta] = None) -> RecordBatchReader
Execute the query and return the results as a pyarrow RecordBatchReader

Parameters:

batch_size (Optional[int], default: None ) – The maximum number of selected records in a RecordBatch object.
timeout (Optional[timedelta], default: None ) – The maximum time to wait for the query to complete. If None, wait indefinitely.
to_list
¶

to_list(*, timeout: Optional[timedelta] = None) -> List[dict]
Execute the query and return the results as a list of dictionaries.

Each list entry is a dictionary with the selected column names as keys, or all table columns if select is not called. The vector and the "_distance" fields are returned whether or not they're explicitly selected.

Parameters:

timeout (Optional[timedelta], default: None ) – The maximum time to wait for the query to complete. If None, wait indefinitely.
to_pydantic
¶

to_pydantic(model: type[T], *, timeout: Optional[timedelta] = None) -> list[T]
Return the table as a list of pydantic models.

Parameters:

model (type[T]) – The pydantic model to use.
timeout (Optional[timedelta], default: None ) – The maximum time to wait for the query to complete. If None, wait indefinitely.
Returns:

List[LanceModel] –
to_polars
¶

to_polars(*, timeout: Optional[timedelta] = None) -> 'pl.DataFrame'
Execute the query and return the results as a Polars DataFrame. In addition to the selected columns, LanceDB also returns a vector and also the "_distance" column which is the distance between the query vector and the returned vector.

Parameters:

timeout (Optional[timedelta], default: None ) – The maximum time to wait for the query to complete. If None, wait indefinitely.
limit
¶

limit(limit: Union[int, None]) -> Self
Set the maximum number of results to return.

Parameters:

limit (Union[int, None]) – The maximum number of results to return. The default query limit is 10 results. For ANN/KNN queries, you must specify a limit. For plain searches, all records are returned if limit not set. WARNING if you have a large dataset, setting the limit to a large number, e.g. the table size, can potentially result in reading a large amount of data into memory and cause out of memory issues.
Returns:

LanceQueryBuilder – The LanceQueryBuilder object.
offset
¶

offset(offset: int) -> Self
Set the offset for the results.

Parameters:

offset (int) – The offset to start fetching results from.
Returns:

LanceQueryBuilder – The LanceQueryBuilder object.
select
¶

select(columns: Union[list[str], dict[str, Union[str, Expr]]]) -> Self
Set the columns to return.

Parameters:

columns (Union[list[str], dict[str, Union[str, Expr]]]) – List of column names to be fetched. Or a dictionary of column names to SQL expressions or :class:~lancedb.expr.Expr objects. All columns are fetched if None or unspecified.
Returns:

LanceQueryBuilder – The LanceQueryBuilder object.
where
¶

where(where: Union[str, Expr], prefilter: bool = True) -> Self
Set the where clause.

Parameters:

where (Union[str, Expr]) – The filter condition. Can be a SQL string or a type-safe :class:~lancedb.expr.Expr built with :func:~lancedb.expr.col and :func:~lancedb.expr.lit.
prefilter (bool, default: True ) – If True, apply the filter before vector search, otherwise the filter is applied on the result of vector search. This feature is EXPERIMENTAL and may be removed and modified without warning in the future.
Returns:

LanceQueryBuilder – The LanceQueryBuilder object.
Notes
Calling this multiple times combines the filters with a logical AND rather than replacing the previous filter.

with_row_id
¶

with_row_id(with_row_id: bool) -> Self
Set whether to return row ids.

Parameters:

with_row_id (bool) – If True, return _rowid column in the results.
Returns:

LanceQueryBuilder – The LanceQueryBuilder object.
with_row_address
¶

with_row_address(with_row_address: bool = True) -> Self
Set whether to return row addresses.

Parameters:

with_row_address (bool, default: True ) – If True, return the _rowaddr column in the results.
Returns:

LanceQueryBuilder – The LanceQueryBuilder object.
with_fragments
¶

with_fragments(fragments: Any) -> Self
Set the Lance fragments to scan for plain scanner-backed queries.

fragment_ids
¶

fragment_ids(fragment_ids: List[int]) -> Self
Set the Lance fragment ids to scan for plain scanner-backed queries.

explain_plan
¶

explain_plan(verbose: Optional[bool] = False) -> str
Return the execution plan for this query.

Examples:


>>> import lancedb
>>> db = lancedb.connect("./.lancedb")
>>> table = db.create_table("my_table", [{"vector": [99.0, 99]}])
>>> query = [100, 100]
>>> plan = table.search(query).explain_plan(True)
>>> print(plan)
ProjectionExec: expr=[vector@0 as vector, _distance@2 as _distance]
  GlobalLimitExec: skip=0, fetch=10
    FilterExec: _distance@2 IS NOT NULL
      SortExec: TopK(fetch=10), expr=[_distance@2 ASC NULLS LAST, _rowid@1 ASC NULLS LAST], preserve_partitioning=[false]
        KNNVectorDistance: metric=l2
          LanceRead: uri=..., projection=[vector], ...
Parameters:

verbose (bool, default: False ) – Use a verbose output format.
Returns:

plan ( str ) –
order_by
¶

order_by(ordering: Optional[List[ColumnOrdering]]) -> Self
Set the ordering for the results.

Parameters:

ordering (Optional[List[ColumnOrdering]]) – The ordering to use for the results. If None, then the default ordering will be used.
Returns:

LanceQueryBuilder – The LanceQueryBuilder object.
analyze_plan
¶

analyze_plan() -> str
Run the query and return its execution plan with runtime metrics.

This returns detailed metrics for each step, such as elapsed time, rows processed, bytes read, and I/O stats. It is useful for debugging and performance tuning.

Examples:


>>> import lancedb
>>> db = lancedb.connect("./.lancedb")
>>> table = db.create_table("my_table", [{"vector": [99.0, 99]}])
>>> query = [100, 100]
>>> plan = table.search(query).analyze_plan()
>>> print(plan)
AnalyzeExec verbose=true, elapsed=..., metrics=...
  TracedExec, elapsed=..., metrics=...
    ProjectionExec: elapsed=..., expr=[...],
    metrics=[output_rows=..., elapsed_compute=..., output_bytes=...]
      GlobalLimitExec: elapsed=..., skip=0, fetch=10,
      metrics=[output_rows=..., elapsed_compute=..., output_bytes=...]
        FilterExec: elapsed=..., _distance@2 IS NOT NULL, metrics=[...]
          SortExec: elapsed=..., TopK(fetch=10), expr=[...],
          preserve_partitioning=[...],
          metrics=[output_rows=..., elapsed_compute=...,
          output_bytes=..., row_replacements=...]
            KNNVectorDistance: elapsed=..., metric=l2,
            metrics=[output_rows=..., elapsed_compute=...,
            output_bytes=..., output_batches=...]
              LanceRead: elapsed=..., uri=..., projection=[vector],
              num_fragments=..., range_before=None, range_after=None,
              row_id=true, row_addr=false,
              full_filter=--, refine_filter=--,
              metrics=[output_rows=..., elapsed_compute=..., output_bytes=...,
              fragments_scanned=..., ranges_scanned=1, rows_scanned=1,
              bytes_read=..., iops=..., requests=..., task_wait_time=...]
Returns:

plan ( str ) – The physical query execution plan with runtime metrics.
vector
¶

vector(vector: Union[ndarray, list]) -> Self
Set the vector to search for.

Parameters:

vector (Union[ndarray, list]) – The vector to search for.
Returns:

LanceQueryBuilder – The LanceQueryBuilder object.
text
¶

text(text: str | FullTextQuery) -> Self
Set the text to search for.

Parameters:

text (str | FullTextQuery) – If a string, it is treated as a MatchQuery. If a FullTextQuery object, it is used directly.
Returns:

LanceQueryBuilder – The LanceQueryBuilder object.
rerank
¶

rerank(reranker: Reranker) -> Self
Rerank the results using the specified reranker.

Parameters:

reranker (Reranker) – The reranker to use.
Returns:

The LanceQueryBuilder object. –
to_query_object
¶

to_query_object() -> Query
Return a serializable representation of the query

Returns:

Query – The serializable representation of the query
lancedb.query.LanceVectorQueryBuilder
¶
Bases: LanceQueryBuilder

Examples:


>>> import lancedb
>>> data = [{"vector": [1.1, 1.2], "b": 2},
...         {"vector": [0.5, 1.3], "b": 4},
...         {"vector": [0.4, 0.4], "b": 6},
...         {"vector": [0.4, 0.4], "b": 10}]
>>> db = lancedb.connect("./.lancedb")
>>> table = db.create_table("my_table", data=data)
>>> (table.search([0.4, 0.4])
...       .distance_type("cosine")
...       .where("b < 10")
...       .select(["b", "vector"])
...       .limit(2)
...       .to_pandas())
   b      vector  _distance
0  6  [0.4, 0.4]   0.000000
1  2  [1.1, 1.2]   0.000944
metric
¶

metric(metric: Literal['l2', 'cosine', 'dot']) -> LanceVectorQueryBuilder
Set the distance metric to use.

This is an alias for distance_type() and may be deprecated in the future. Please use distance_type() instead.

Parameters:

metric (Literal['l2', 'cosine', 'dot']) – The distance metric to use. By default "l2" is used.
Returns:

LanceVectorQueryBuilder – The LanceQueryBuilder object.
distance_type
¶

distance_type(distance_type: Literal['l2', 'cosine', 'dot']) -> 'LanceVectorQueryBuilder'
Set the distance metric to use.

When performing a vector search we try and find the "nearest" vectors according to some kind of distance metric. This parameter controls which distance metric to use.

Note: if there is a vector index then the distance type used MUST match the distance type used to train the vector index. If this is not done then the results will be invalid.

Parameters:

distance_type (Literal['l2', 'cosine', 'dot']) – The distance metric to use. By default "l2" is used.
Returns:

LanceVectorQueryBuilder – The LanceQueryBuilder object.
nprobes
¶

nprobes(nprobes: int) -> LanceVectorQueryBuilder
Set the number of probes to use.

Higher values will yield better recall (more likely to find vectors if they exist) at the expense of latency.

See discussion in [Querying an ANN Index][querying-an-ann-index] for tuning advice.

This method sets both the minimum and maximum number of probes to the same value. See minimum_nprobes and maximum_nprobes for more fine-grained control.

Parameters:

nprobes (int) – The number of probes to use.
Returns:

LanceVectorQueryBuilder – The LanceQueryBuilder object.
minimum_nprobes
¶

minimum_nprobes(minimum_nprobes: int) -> LanceVectorQueryBuilder
Set the minimum number of probes to use.

See nprobes for more details.

These partitions will be searched on every vector query and will increase recall at the expense of latency.

maximum_nprobes
¶

maximum_nprobes(maximum_nprobes: int) -> LanceVectorQueryBuilder
Set the maximum number of probes to use.

See nprobes for more details.

If this value is greater than minimum_nprobes then the excess partitions will be searched only if we have not found enough results.

This can be useful when there is a narrow filter to allow these queries to spend more time searching and avoid potential false negatives.

If this value is 0 then no limit will be applied and all partitions could be searched if needed to satisfy the limit.

distance_range
¶

distance_range(lower_bound: Optional[float] = None, upper_bound: Optional[float] = None) -> LanceVectorQueryBuilder
Set the distance range to use.

Only rows with distances within range [lower_bound, upper_bound) will be returned.

Parameters:

lower_bound (Optional[float], default: None ) – The lower bound of the distance range.
upper_bound (Optional[float], default: None ) – The upper bound of the distance range.
Returns:

LanceVectorQueryBuilder – The LanceQueryBuilder object.
ef
¶

ef(ef: int) -> LanceVectorQueryBuilder
Set the number of candidates to consider during search.

Higher values will yield better recall (more likely to find vectors if they exist) at the expense of latency.

This only applies to the HNSW-related index. The default value is 1.5 * limit.

Parameters:

ef (int) – The number of candidates to consider during search.
Returns:

LanceVectorQueryBuilder – The LanceQueryBuilder object.
refine_factor
¶

refine_factor(refine_factor: int) -> LanceVectorQueryBuilder
Set the refine factor to use, increasing the number of vectors sampled.

As an example, a refine factor of 2 will sample 2x as many vectors as requested, re-ranks them, and returns the top half most relevant results.

See discussion in [Querying an ANN Index][querying-an-ann-index] for tuning advice.

Parameters:

refine_factor (int) – The refine factor to use.
Returns:

LanceVectorQueryBuilder – The LanceQueryBuilder object.
output_schema
¶

output_schema() -> Schema
Return the output schema for the query

This does not execute the query.

to_arrow
¶

to_arrow(*, timeout: Optional[timedelta] = None) -> Table
Execute the query and return the results as an Apache Arrow Table.

In addition to the selected columns, LanceDB also returns a vector and also the "_distance" column which is the distance between the query vector and the returned vectors.

Parameters:

timeout (Optional[timedelta], default: None ) – The maximum time to wait for the query to complete. If None, wait indefinitely.
to_query_object
¶

to_query_object() -> Query
Build a Query object

This can be used to serialize a query

to_batches
¶

to_batches(batch_size: Optional[int] = None, *, timeout: Optional[timedelta] = None) -> RecordBatchReader
Execute the query and return the result as a RecordBatchReader object.

Parameters:

batch_size (Optional[int], default: None ) – The maximum number of selected records in a RecordBatch object.
timeout (Optional[timedelta], default: None ) – The maximum time to wait for the query to complete. If None, wait indefinitely.
Returns:

RecordBatchReader –
where
¶

where(where: Union[str, Expr], prefilter: bool = None) -> LanceVectorQueryBuilder
Set the where clause.

Parameters:

where (Union[str, Expr]) – The filter condition. Can be a SQL string or a type-safe :class:~lancedb.expr.Expr built with :func:~lancedb.expr.col and :func:~lancedb.expr.lit.
prefilter (bool, default: None ) – If True, apply the filter before vector search, otherwise the filter is applied on the result of vector search.
Returns:

LanceQueryBuilder – The LanceQueryBuilder object.
Notes
Calling this multiple times combines the filters with a logical AND rather than replacing the previous filter.

rerank
¶

rerank(reranker: Reranker, query_string: Optional[str] = None) -> LanceVectorQueryBuilder
Rerank the results using the specified reranker.

Parameters:

reranker (Reranker) – The reranker to use.
query_string (Optional[str], default: None ) – The query to use for reranking. This needs to be specified explicitly here as the query used for vector search may already be vectorized and the reranker requires a string query. This is only required if the query used for vector search is not a string. Note: This doesn't yet support the case where the query is multimodal or a list of vectors.
Returns:

LanceVectorQueryBuilder – The LanceQueryBuilder object.
bypass_vector_index
¶

bypass_vector_index() -> LanceVectorQueryBuilder
If this is called then any vector index is skipped

An exhaustive (flat) search will be performed. The query vector will be compared to every vector in the table. At high scales this can be expensive. However, this is often still useful. For example, skipping the vector index can give you ground truth results which you can use to calculate your recall to select an appropriate value for nprobes.

Returns:

LanceVectorQueryBuilder – The LanceVectorQueryBuilder object.
fast_search
¶

fast_search() -> LanceVectorQueryBuilder
Skip a flat search of unindexed data. This will improve search performance but search results will not include unindexed data.

Returns:

LanceVectorQueryBuilder – The LanceVectorQueryBuilder object.
lancedb.query.LanceFtsQueryBuilder
¶
Bases: LanceQueryBuilder

A builder for full text search for LanceDB.

phrase_query
¶

phrase_query(phrase_query: bool = True) -> LanceFtsQueryBuilder
Set whether to use phrase query.

Parameters:

phrase_query (bool, default: True ) – If True, then the query will be wrapped in quotes and double quotes replaced by single quotes.
Returns:

LanceFtsQueryBuilder – The LanceFtsQueryBuilder object.
fast_search
¶

fast_search() -> LanceFtsQueryBuilder
Skip a flat search of unindexed data. This will improve search performance but search results will not include unindexed data.

Returns:

LanceFtsQueryBuilder – The LanceFtsQueryBuilder object.
to_query_object
¶

to_query_object() -> Query
output_schema
¶

output_schema() -> Schema
Return the output schema for the query

This does not execute the query.

to_arrow
¶

to_arrow(*, timeout: Optional[timedelta] = None) -> Table
to_batches
¶

to_batches(batch_size: Optional[int] = None, timeout: Optional[timedelta] = None)
rerank
¶

rerank(reranker: Reranker) -> LanceFtsQueryBuilder
Rerank the results using the specified reranker.

Parameters:

reranker (Reranker) – The reranker to use.
Returns:

LanceFtsQueryBuilder – The LanceQueryBuilder object.
lancedb.query.LanceHybridQueryBuilder
¶
Bases: LanceQueryBuilder

A query builder that performs hybrid vector and full text search. Results are combined and reranked based on the specified reranker. By default, the results are reranked using the RRFReranker, which uses reciprocal rank fusion score for reranking.

To make the vector and fts results comparable, the scores are normalized. Instead of normalizing scores, the normalize parameter can be set to "rank" in the rerank method to convert the scores to ranks and then normalize them.

phrase_query
¶

phrase_query(phrase_query: bool = None) -> LanceHybridQueryBuilder
Set whether to use phrase query.

Parameters:

phrase_query (bool, default: None ) – If True, then the query will be wrapped in quotes and double quotes replaced by single quotes.
Returns:

LanceHybridQueryBuilder – The LanceHybridQueryBuilder object.
to_query_object
¶

to_query_object() -> Query
to_arrow
¶

to_arrow(*, timeout: Optional[timedelta] = None) -> Table
to_batches
¶

to_batches(batch_size: Optional[int] = None, timeout: Optional[timedelta] = None)
rerank
¶

rerank(reranker: Reranker = RRFReranker(), normalize: str = 'score') -> LanceHybridQueryBuilder
Rerank the hybrid search results using the specified reranker. The reranker must be an instance of Reranker class.

Parameters:

reranker (Reranker, default: RRFReranker() ) – The reranker to use. Must be an instance of Reranker class.
normalize (str, default: 'score' ) – The method to normalize the scores. Can be "rank" or "score". If "rank", the scores are converted to ranks and then normalized. If "score", the scores are normalized directly.
Returns:

LanceHybridQueryBuilder – The LanceHybridQueryBuilder object.
nprobes
¶

nprobes(nprobes: int) -> LanceHybridQueryBuilder
Set the number of probes to use for vector search.

Higher values will yield better recall (more likely to find vectors if they exist) at the expense of latency.

Parameters:

nprobes (int) – The number of probes to use.
Returns:

LanceHybridQueryBuilder – The LanceHybridQueryBuilder object.
minimum_nprobes
¶

minimum_nprobes(minimum_nprobes: int) -> LanceHybridQueryBuilder
Set the minimum number of probes to use.

See nprobes for more details.

maximum_nprobes
¶

maximum_nprobes(maximum_nprobes: int) -> LanceHybridQueryBuilder
Set the maximum number of probes to use.

See nprobes for more details.

distance_range
¶

distance_range(lower_bound: Optional[float] = None, upper_bound: Optional[float] = None) -> LanceHybridQueryBuilder
Set the distance range to use.

Only rows with distances within range [lower_bound, upper_bound) will be returned.

Parameters:

lower_bound (Optional[float], default: None ) – The lower bound of the distance range.
upper_bound (Optional[float], default: None ) – The upper bound of the distance range.
Returns:

LanceHybridQueryBuilder – The LanceHybridQueryBuilder object.
ef
¶

ef(ef: int) -> LanceHybridQueryBuilder
Set the number of candidates to consider during search.

Higher values will yield better recall (more likely to find vectors if they exist) at the expense of latency.

This only applies to the HNSW-related index. The default value is 1.5 * limit.

Parameters:

ef (int) – The number of candidates to consider during search.
Returns:

LanceHybridQueryBuilder – The LanceHybridQueryBuilder object.
metric
¶

metric(metric: Literal['l2', 'cosine', 'dot']) -> LanceHybridQueryBuilder
Set the distance metric to use.

This is an alias for distance_type() and may be deprecated in the future. Please use distance_type() instead.

Parameters:

metric (Literal['l2', 'cosine', 'dot']) – The distance metric to use. By default "l2" is used.
Returns:

LanceVectorQueryBuilder – The LanceQueryBuilder object.
distance_type
¶

distance_type(distance_type: Literal['l2', 'cosine', 'dot']) -> 'LanceHybridQueryBuilder'
Set the distance metric to use.

When performing a vector search we try and find the "nearest" vectors according to some kind of distance metric. This parameter controls which distance metric to use.

Note: if there is a vector index then the distance type used MUST match the distance type used to train the vector index. If this is not done then the results will be invalid.

Parameters:

distance_type (Literal['l2', 'cosine', 'dot']) – The distance metric to use. By default "l2" is used.
Returns:

LanceVectorQueryBuilder – The LanceQueryBuilder object.
refine_factor
¶

refine_factor(refine_factor: int) -> LanceHybridQueryBuilder
Refine the vector search results by reading extra elements and re-ranking them in memory.

Parameters:

refine_factor (int) – The refine factor to use.
Returns:

LanceHybridQueryBuilder – The LanceHybridQueryBuilder object.
vector
¶

vector(vector: Union[ndarray, list]) -> LanceHybridQueryBuilder
text
¶

text(text: str | FullTextQuery) -> LanceHybridQueryBuilder
bypass_vector_index
¶

bypass_vector_index() -> LanceHybridQueryBuilder
If this is called then any vector index is skipped

An exhaustive (flat) search will be performed. The query vector will be compared to every vector in the table. At high scales this can be expensive. However, this is often still useful. For example, skipping the vector index can give you ground truth results which you can use to calculate your recall to select an appropriate value for nprobes.

Returns:

LanceHybridQueryBuilder – The LanceHybridQueryBuilder object.
explain_plan
¶

explain_plan(verbose: Optional[bool] = False) -> str
Return the execution plan for this query.

Examples:


>>> import lancedb
>>> db = lancedb.connect("./.lancedb")
>>> table = db.create_table("my_table", [{"vector": [99.0, 99]}])
>>> query = [100, 100]
>>> plan = table.search(query).explain_plan(True)
>>> print(plan)
ProjectionExec: expr=[vector@0 as vector, _distance@2 as _distance]
  GlobalLimitExec: skip=0, fetch=10
    FilterExec: _distance@2 IS NOT NULL
      SortExec: TopK(fetch=10), expr=[_distance@2 ASC NULLS LAST, _rowid@1 ASC NULLS LAST], preserve_partitioning=[false]
        KNNVectorDistance: metric=l2
          LanceRead: uri=..., projection=[vector], ...
Parameters:

verbose (bool, default: False ) – Use a verbose output format.
Returns:

plan ( str ) –
analyze_plan
¶

analyze_plan()
Execute the query and display with runtime metrics.

Returns:

plan ( str ) –
Embeddings
¶
lancedb.embeddings.registry.EmbeddingFunctionRegistry
¶
This is a singleton class used to register embedding functions and fetch them by name. It also handles serializing and deserializing. You can implement your own embedding function by subclassing EmbeddingFunction or TextEmbeddingFunction and registering it with the registry.

NOTE: Here TEXT is a type alias for Union[str, List[str], pa.Array, pa.ChunkedArray, np.ndarray]

Examples:


>>> registry = EmbeddingFunctionRegistry.get_instance()
>>> @registry.register("my-embedding-function")
... class MyEmbeddingFunction(EmbeddingFunction):
...     def ndims(self) -> int:
...         return 128
...
...     def compute_query_embeddings(self, query: str, *args, **kwargs):
...         return self.compute_source_embeddings(query, *args, **kwargs)
...
...     def compute_source_embeddings(self, texts, *args, **kwargs):
...         return [np.random.rand(self.ndims()) for _ in range(len(texts))]
...
>>> registry.get("my-embedding-function")
<class 'lancedb.embeddings.registry.MyEmbeddingFunction'>
get_instance
¶

get_instance()
register
¶

register(alias: Optional[str] = None)
This creates a decorator that can be used to register an EmbeddingFunction.

Parameters:

alias (Optional[str], default: None ) – a human friendly name for the embedding function. If not provided, the class name will be used.
reset
¶

reset()
Reset the registry to its initial state

get
¶

get(name: str) -> Type[EmbeddingFunction]
Fetch an embedding function class by name

Parameters:

name (str) – The name of the embedding function to fetch Either the alias or the class name if no alias was provided during registration
parse_functions
¶

parse_functions(metadata: Optional[Dict[bytes, bytes]]) -> Dict[str, EmbeddingFunctionConfig]
Parse the metadata from an arrow table and return a mapping of the vector column to the embedding function and source column

Parameters:

metadata (Optional[Dict[bytes, bytes]]) – The metadata from an arrow table. Note that the keys and values are bytes (pyarrow api)
Returns:

functions ( dict ) – A mapping of vector column name to embedding function. An empty dict is returned if input is None or does not contain b"embedding_functions".
function_to_metadata
¶

function_to_metadata(conf: EmbeddingFunctionConfig)
Convert the given embedding function and source / vector column configs into a config dictionary that can be serialized into arrow metadata

get_table_metadata
¶

get_table_metadata(func_list)
Convert a list of embedding functions and source / vector configs into a config dictionary that can be serialized into arrow metadata

set_var
¶

set_var(name: str, value: str) -> None
Set a variable. These can be accessed in embedding configuration using the syntax $var:variable_name. If they are not set, an error will be thrown letting you know which variable is missing. If you want to supply a default value, you can add an additional part in the configuration like so: $var:variable_name:default_value. Default values can be used for runtime configurations that are not sensitive, such as whether to use a GPU for inference.

The name must not contain a colon. Default values can contain colons.

get_var
¶

get_var(name: str) -> str
Get a variable.

lancedb.embeddings.base.EmbeddingFunctionConfig
¶
Bases: BaseModel

This model encapsulates the configuration for a embedding function in a lancedb table. It holds the embedding function, the source column, and the vector column

vector_column
¶

vector_column: str
source_column
¶

source_column: str
function
¶

function: EmbeddingFunction
lancedb.embeddings.base.EmbeddingFunction
¶
Bases: BaseModel, ABC

An ABC for embedding functions.

All concrete embedding functions must implement the following methods: 1. compute_query_embeddings() which takes a query and returns a list of embeddings 2. compute_source_embeddings() which returns a list of embeddings for the source column For text data, the two will be the same. For multi-modal data, the source column might be images and the vector column might be text. 3. ndims() which returns the number of dimensions of the vector column

max_retries
¶

max_retries: int = 7
create
¶

create(**kwargs)
Create an instance of the embedding function

sensitive_keys
¶

sensitive_keys() -> List[str]
Return a list of keys that are sensitive and should not be allowed to be set to hardcoded values in the config. For example, API keys.

compute_query_embeddings
¶

compute_query_embeddings(*args, **kwargs) -> list[Union[array, None]]
Compute the embeddings for a given user query

Returns:

A list of embeddings for each input. The embedding of each input can be None –
when the embedding is not valid. –
compute_source_embeddings
¶

compute_source_embeddings(*args, **kwargs) -> list[Union[array, None]]
Compute the embeddings for the source column in the database

Returns:

A list of embeddings for each input. The embedding of each input can be None –
when the embedding is not valid. –
compute_query_embeddings_with_retry
¶

compute_query_embeddings_with_retry(*args, **kwargs) -> list[Union[array, None]]
Compute the embeddings for a given user query with retries

Returns:

A list of embeddings for each input. The embedding of each input can be None –
when the embedding is not valid. –
compute_source_embeddings_with_retry
¶

compute_source_embeddings_with_retry(*args, **kwargs) -> list[Union[array, None]]
Compute the embeddings for the source column in the database with retries.

Returns:

A list of embeddings for each input. The embedding of each input can be None –
when the embedding is not valid. –
sanitize_input
¶

sanitize_input(texts: TEXT) -> Union[List[str], ndarray]
Sanitize the input to the embedding function.

safe_model_dump
¶

safe_model_dump()
ndims
¶

ndims() -> int
Return the dimensions of the vector column

SourceField
¶

SourceField(**kwargs)
Creates a pydantic Field that can automatically annotate the source column for this embedding function

VectorField
¶

VectorField(**kwargs)
Creates a pydantic Field that can automatically annotate the target vector column for this embedding function

lancedb.embeddings.base.TextEmbeddingFunction
¶
Bases: EmbeddingFunction

A callable ABC for embedding functions that take text as input

compute_query_embeddings
¶

compute_query_embeddings(query: str, *args, **kwargs) -> list[Union[array, None]]
compute_source_embeddings
¶

compute_source_embeddings(texts: TEXT, *args, **kwargs) -> list[Union[array, None]]
generate_embeddings
¶

generate_embeddings(texts: Union[List[str], ndarray], *args, **kwargs) -> list[Union[array, None]]
Generate the embeddings for the given texts

lancedb.embeddings.sentence_transformers.SentenceTransformerEmbeddings
¶
Bases: TextEmbeddingFunction

An embedding function that uses the sentence-transformers library

https://huggingface.co/sentence-transformers

Parameters:

name – The name of the model to use.
device – The device to use for the model
normalize – Whether to normalize the embeddings
trust_remote_code – Whether to trust the remote code
name
¶

name: str = 'all-MiniLM-L6-v2'
device
¶

device: str = 'cpu'
normalize
¶

normalize: bool = True
trust_remote_code
¶

trust_remote_code: bool = True
embedding_model
¶

embedding_model
Get the sentence-transformers embedding model specified by the name, device, and trust_remote_code. This is cached so that the model is only loaded once per process.

ndims
¶

ndims()
generate_embeddings
¶

generate_embeddings(texts: Union[List[str], ndarray]) -> List[array]
Get the embeddings for the given texts

Parameters:

texts (Union[List[str], ndarray]) – The texts to embed
get_embedding_model
¶

get_embedding_model()
Get the sentence-transformers embedding model specified by the name, device, and trust_remote_code. This is cached so that the model is only loaded once per process.

TODO: use lru_cache instead with a reasonable/configurable maxsize

lancedb.embeddings.openai.OpenAIEmbeddings
¶
Bases: TextEmbeddingFunction

An embedding function that uses the OpenAI API

https://platform.openai.com/docs/guides/embeddings

This can also be used for open source models that are compatible with the OpenAI API.

Notes
If you're running an Ollama server locally, you can just override the base_url parameter and provide the Ollama embedding model you want to use (https://ollama.com/library):


from lancedb.embeddings import get_registry
openai = get_registry().get("openai")
embedding_function = openai.create(
    name="<ollama-embedding-model-name>",
    base_url="http://localhost:11434",
    )
name
¶

name: str = 'text-embedding-ada-002'
dim
¶

dim: Optional[int] = None
base_url
¶

base_url: Optional[str] = None
default_headers
¶

default_headers: Optional[dict] = None
organization
¶

organization: Optional[str] = None
api_key
¶

api_key: Optional[str] = None
use_azure
¶

use_azure: bool = False
ndims
¶

ndims()
sensitive_keys
¶

sensitive_keys()
model_names
¶

model_names()
generate_embeddings
¶

generate_embeddings(texts: Union[List[str], ndarray]) -> List[array]
Get the embeddings for the given texts

Parameters:

texts (Union[List[str], ndarray]) – The texts to embed
lancedb.embeddings.open_clip.OpenClipEmbeddings
¶
Bases: EmbeddingFunction

An embedding function that uses the OpenClip API For multi-modal text-to-image search

https://github.com/mlfoundations/open_clip

name
¶

name: str = 'ViT-B-32'
pretrained
¶

pretrained: str = 'laion2b_s34b_b79k'
device
¶

device: str = 'cpu'
batch_size
¶

batch_size: int = 64
normalize
¶

normalize: bool = True
ndims
¶

ndims()
compute_query_embeddings
¶

compute_query_embeddings(query: Union[str, Image], *args, **kwargs) -> List[ndarray]
Compute the embeddings for a given user query

Parameters:

query (Union[str, Image]) – The query to embed. A query can be either text or an image.
generate_text_embeddings
¶

generate_text_embeddings(text: str) -> ndarray
sanitize_input
¶

sanitize_input(images: IMAGES) -> Union[List[bytes], ndarray]
Sanitize the input to the embedding function.

compute_source_embeddings
¶

compute_source_embeddings(images: IMAGES, *args, **kwargs) -> List[array]
Get the embeddings for the given images

generate_image_embedding
¶

generate_image_embedding(image: Union[str, bytes, Image]) -> ndarray
Generate the embedding for a single image

Parameters:

image (Union[str, bytes, Image]) – The image to embed. If the image is a str, it is treated as a uri. If the image is bytes, it is treated as the raw image bytes.
Remote configuration
¶
lancedb.remote.ClientConfig
¶
Configuration for the LanceDB Cloud HTTP client.

user_agent
¶

user_agent: str = f'LanceDB-Python-Client/{__version__}'
retry_config
¶

retry_config: RetryConfig = field(default_factory=RetryConfig)
timeout_config
¶

timeout_config: Optional[TimeoutConfig] = field(default_factory=TimeoutConfig)
extra_headers
¶

extra_headers: Optional[dict] = None
id_delimiter
¶

id_delimiter: Optional[str] = None
tls_config
¶

tls_config: Optional[TlsConfig] = None
header_provider
¶

header_provider: Optional[HeaderProvider] = None
user_id
¶

user_id: Optional[str] = None
lancedb.remote.TimeoutConfig
¶
Timeout configuration for remote HTTP client.

timeout
¶

timeout: Optional[timedelta] = None
connect_timeout
¶

connect_timeout: Optional[timedelta] = None
read_timeout
¶

read_timeout: Optional[timedelta] = None
pool_idle_timeout
¶

pool_idle_timeout: Optional[timedelta] = None
lancedb.remote.RetryConfig
¶
Retry configuration for the remote HTTP client.

retries
¶

retries: Optional[int] = None
connect_retries
¶

connect_retries: Optional[int] = None
read_retries
¶

read_retries: Optional[int] = None
backoff_factor
¶

backoff_factor: Optional[float] = None
backoff_jitter
¶

backoff_jitter: Optional[float] = None
statuses
¶

statuses: Optional[List[int]] = None
Context
¶
lancedb.context.contextualize
¶

contextualize(raw_df: 'pd.DataFrame') -> Contextualizer
Create a Contextualizer object for the given DataFrame.

Used to create context windows. Context windows are rolling subsets of text data.

The input text column should already be separated into rows that will be the unit of the window. So to create a context window over tokens, start with a DataFrame with one token per row. To create a context window over sentences, start with a DataFrame with one sentence per row.

Examples:


>>> from lancedb.context import contextualize
>>> import pandas as pd
>>> data = pd.DataFrame({
...    'token': ['The', 'quick', 'brown', 'fox', 'jumped', 'over',
...              'the', 'lazy', 'dog', 'I', 'love', 'sandwiches'],
...    'document_id': [1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2]
... })
window determines how many rows to include in each window. In our case this how many tokens, but depending on the input data, it could be sentences, paragraphs, messages, etc.


>>> contextualize(data).window(3).stride(1).text_col('token').to_pandas()
                token  document_id
0     The quick brown            1
1     quick brown fox            1
2    brown fox jumped            1
3     fox jumped over            1
4     jumped over the            1
5       over the lazy            1
6        the lazy dog            1
7          lazy dog I            1
8          dog I love            1
9   I love sandwiches            2
10    love sandwiches            2
>>> (contextualize(data).window(7).stride(1).min_window_size(7)
...   .text_col('token').to_pandas())
                                  token  document_id
0   The quick brown fox jumped over the            1
1  quick brown fox jumped over the lazy            1
2    brown fox jumped over the lazy dog            1
3        fox jumped over the lazy dog I            1
4       jumped over the lazy dog I love            1
5   over the lazy dog I love sandwiches            1
stride determines how many rows to skip between each window start. This can be used to reduce the total number of windows generated.


>>> contextualize(data).window(4).stride(2).text_col('token').to_pandas()
                    token  document_id
0     The quick brown fox            1
2   brown fox jumped over            1
4    jumped over the lazy            1
6          the lazy dog I            1
8   dog I love sandwiches            1
10        love sandwiches            2
groupby determines how to group the rows. For example, we would like to have context windows that don't cross document boundaries. In this case, we can pass document_id as the group by.


>>> (contextualize(data)
...     .window(4).stride(2).text_col('token').groupby('document_id')
...     .to_pandas())
                   token  document_id
0    The quick brown fox            1
2  brown fox jumped over            1
4   jumped over the lazy            1
6           the lazy dog            1
9      I love sandwiches            2
min_window_size determines the minimum size of the context windows that are generated.This can be used to trim the last few context windows which have size less than min_window_size. By default context windows of size 1 are skipped.


>>> (contextualize(data)
...     .window(6).stride(3).text_col('token').groupby('document_id')
...     .to_pandas())
                             token  document_id
0  The quick brown fox jumped over            1
3     fox jumped over the lazy dog            1
6                     the lazy dog            1
9                I love sandwiches            2

>>> (contextualize(data)
...     .window(6).stride(3).min_window_size(4).text_col('token')
...     .groupby('document_id')
...     .to_pandas())
                             token  document_id
0  The quick brown fox jumped over            1
3     fox jumped over the lazy dog            1
lancedb.context.Contextualizer
¶
Create context windows from a DataFrame. See lancedb.context.contextualize.

window
¶

window(window: int) -> Contextualizer
Set the window size. i.e., how many rows to include in each window.

Parameters:

window (int) – The window size.
stride
¶

stride(stride: int) -> Contextualizer
Set the stride. i.e., how many rows to skip between each window.

Parameters:

stride (int) – The stride.
groupby
¶

groupby(groupby: str) -> Contextualizer
Set the groupby column. i.e., how to group the rows. Windows don't cross groups

Parameters:

groupby (str) – The groupby column.
text_col
¶

text_col(text_col: str) -> Contextualizer
Set the text column used to make the context window.

Parameters:

text_col (str) – The text column.
min_window_size
¶

min_window_size(min_window_size: int) -> Contextualizer
Set the (optional) min_window_size size for the context window.

Parameters:

min_window_size (int) – The min_window_size.
to_df
¶

to_df() -> 'pd.DataFrame'
to_pandas
¶

to_pandas() -> 'pd.DataFrame'
Create the context windows and return a DataFrame.

Full text search
¶
Use lancedb.table.Table.create_fts_index for the synchronous API or lancedb.table.AsyncTable.create_index with lancedb.index.FTS for the asynchronous API.

lancedb.index.FTS
¶
Describe a FTS index configuration.

FTS is a full-text search index that can be used on String columns

For example, it works with title, description, content, etc.

Notes
Model-backed tokenizers such as jieba/default and lindera/ipadic require tokenizer models in Lance's language model home. Set LANCE_LANGUAGE_MODEL_HOME to override the default platform data directory under lance/language_models.

with_position
¶

with_position: bool = False
base_tokenizer
¶

base_tokenizer: BaseTokenizerType = 'simple'
language
¶

language: str = 'English'
max_token_length
¶

max_token_length: Optional[int] = 40
lower_case
¶

lower_case: bool = True
stem
¶

stem: bool = True
remove_stop_words
¶

remove_stop_words: bool = True
ascii_folding
¶

ascii_folding: bool = True
ngram_min_length
¶

ngram_min_length: int = 3
ngram_max_length
¶

ngram_max_length: int = 3
prefix_only
¶

prefix_only: bool = False
Utilities
¶
lancedb.schema.vector
¶

vector(dimension: int, value_type: DataType = pa.float32()) -> DataType
A help function to create a vector type.

Parameters:

dimension (int) –
value_type (DataType, default: float32() ) – The type of the value in the vector.
Returns:

A PyArrow DataType for vectors. –
Examples:


>>> import pyarrow as pa
>>> import lancedb
>>> schema = pa.schema([
...     pa.field("id", pa.int64()),
...     pa.field("vector", lancedb.vector(756)),
... ])
lancedb.merge.LanceMergeInsertBuilder
¶
Bases: object

Builder for a LanceDB merge insert operation

See merge_insert for more context

when_matched_update_all
¶

when_matched_update_all(*, where: Optional[str] = None) -> LanceMergeInsertBuilder
Rows that exist in both the source table (new data) and the target table (old data) will be updated, replacing the old row with the corresponding matching row.

If there are multiple matches then the behavior is undefined. Currently this causes multiple copies of the row to be created but that behavior is subject to change.

Parameters:

where (Optional[str], default: None ) – An optional filter to limit which rows are updated. Column references in this expression must be prefixed with "target." to refer to the existing table data. For example, to only update rows where the existing color is red, use: where="target.color = 'red'"
when_not_matched_insert_all
¶

when_not_matched_insert_all() -> LanceMergeInsertBuilder
Rows that exist only in the source table (new data) should be inserted into the target table.

when_not_matched_by_source_delete
¶

when_not_matched_by_source_delete(condition: Union[str, Expr, None] = None) -> LanceMergeInsertBuilder
Rows that exist only in the target table (old data) will be deleted. An optional condition can be provided to limit what data is deleted.

Parameters:

condition (Union[str, Expr, None], default: None ) – If None then all such rows will be deleted. Otherwise the condition will be used as a filter to limit what rows are deleted. Can be a SQL string or a type-safe :class:~lancedb.expr.Expr built with :func:~lancedb.expr.col and :func:~lancedb.expr.lit.
use_index
¶

use_index(use_index: bool) -> LanceMergeInsertBuilder
Controls whether to use indexes for the merge operation.

When set to True (the default), the operation will use an index if available on the join key for improved performance. When set to False, it forces a full table scan even if an index exists. This can be useful for benchmarking or when the query optimizer chooses a suboptimal path.

Parameters:

use_index (bool) – Whether to use indices for the merge operation. Defaults to True.
use_lsm_write
¶

use_lsm_write(use_lsm_write: bool) -> LanceMergeInsertBuilder
Controls whether the merge uses the MemWAL LSM write path.

By default (unset), a merge_insert on a table with an LSM write spec is routed through Lance's MemWAL shard writer, and a table without one uses the standard path. Pass False to force the standard path even when a spec is set. Pass True to require a spec — merge_insert raises an error if none is installed.

Parameters:

use_lsm_write (bool) – Whether to use the LSM write path.
validate_single_shard
¶

validate_single_shard(validate_single_shard: bool) -> LanceMergeInsertBuilder
Controls how an LSM merge checks that its input targets a single shard.

When a table has an LSM write spec, every row in a merge_insert call must route to the same shard. When True (the default), every row is inspected to verify this. When False, only the first row is inspected and the shard it routes to is used for the whole input — a faster path for callers that have already pre-sharded their input.

Has no effect on tables without an LSM write spec.

Parameters:

validate_single_shard (bool) – Whether to check every row routes to one shard. Defaults to True.
execute
¶

execute(new_data: DATA, on_bad_vectors: str = 'error', fill_value: float = 0.0, timeout: Optional[timedelta] = None) -> MergeInsertResult
Executes the merge insert operation

Nothing is returned but the Table is updated

Parameters:

new_data (DATA) – New records which will be matched against the existing records to potentially insert or update into the table. This parameter can be anything you use for add
on_bad_vectors (str, default: 'error' ) – What to do if any of the vectors are not the same size or contains NaNs. One of "error", "drop", "fill".
fill_value (float, default: 0.0 ) – The value to use when filling vectors. Only used if on_bad_vectors="fill".
timeout (Optional[timedelta], default: None ) – Maximum time to run the operation before cancelling it.
By default, there is a 30-second timeout that is only enforced after the first attempt. This is to prevent spending too long retrying to resolve conflicts. For example, if a write attempt takes 20 seconds and fails, the second attempt will be cancelled after 10 seconds, hitting the 30-second timeout. However, a write that takes one hour and succeeds on the first attempt will not be cancelled.

When this is set, the timeout is enforced on all attempts, including the first.

Returns:

MergeInsertResult – version: the new version number of the table after doing merge insert.
Integrations
¶
Pydantic
¶
lancedb.pydantic.pydantic_to_schema
¶

pydantic_to_schema(model: Type[BaseModel]) -> Schema
Convert a Pydantic Model to a PyArrow Schema.

Parameters:

model (Type[BaseModel]) – The Pydantic BaseModel to convert to Arrow Schema.
Returns:

Schema – The Arrow Schema
Examples:


>>> from typing import List, Optional
>>> import pydantic
>>> from lancedb.pydantic import pydantic_to_schema, Vector
>>> class FooModel(pydantic.BaseModel):
...     id: int
...     s: str
...     vec: Vector(1536)  # fixed_size_list<item: float32>[1536]
...     li: List[int]
...
>>> schema = pydantic_to_schema(FooModel)
>>> assert schema == pa.schema([
...     pa.field("id", pa.int64(), False),
...     pa.field("s", pa.utf8(), False),
...     pa.field("vec", pa.list_(pa.float32(), 1536)),
...     pa.field("li", pa.list_(pa.int64()), False),
... ])
lancedb.pydantic.vector
¶

vector(dim: int, value_type: DataType = pa.float32())
lancedb.pydantic.LanceModel
¶
Bases: BaseModel

A Pydantic Model base class that can be converted to a LanceDB Table.

Examples:


>>> import lancedb
>>> from lancedb.pydantic import LanceModel, Vector
>>>
>>> class TestModel(LanceModel):
...     name: str
...     vector: Vector(2)
...
>>> db = lancedb.connect("./example")
>>> table = db.create_table("test", schema=TestModel)
>>> table.add([
...     TestModel(name="test", vector=[1.0, 2.0])
... ])
AddResult(version=2)
>>> table.search([0., 0.]).limit(1).to_pydantic(TestModel)
[TestModel(name='test', vector=FixedSizeList(dim=2))]
to_arrow_schema
¶

to_arrow_schema()
Get the Arrow Schema for this model.

field_names
¶

field_names() -> List[str]
Get the field names of this model.

safe_get_fields
¶

safe_get_fields()
parse_embedding_functions
¶

parse_embedding_functions() -> List['EmbeddingFunctionConfig']
Parse the embedding functions from this model.

Reranking
¶
lancedb.rerankers.linear_combination.LinearCombinationReranker
¶
Bases: Reranker

Reranks the results using a linear combination of the scores from the vector and FTS search. For missing scores, fill with fill value.

Parameters:

weight (float, default: 0.7 ) – The weight to give to the vector score. Must be between 0 and 1.
fill (float, default: 1.0 ) – The score to give to results that are only in one of the two result sets. This is treated as penalty, so a higher value means a lower score. TODO: We should just hardcode this-- its pretty confusing as we invert scores to calculate final score
return_score (str, default: "relevance" ) – opntions are "relevance" or "all" The type of score to return. If "relevance", will return only the relevance score. If "all", will return all scores from the vector and FTS search along with the relevance score.
weight
¶

weight = weight
fill
¶

fill = fill
rerank_hybrid
¶

rerank_hybrid(query: str, vector_results: Table, fts_results: Table)
merge_results
¶

merge_results(vector_results: Table, fts_results: Table, fill: float)
lancedb.rerankers.cohere.CohereReranker
¶
Bases: Reranker

Reranks the results using the Cohere Rerank API. https://docs.cohere.com/docs/rerank-guide

Parameters:

model_name (str, default: "rerank-english-v2.0" ) – The name of the cross encoder model to use. Available cohere models are: - rerank-english-v2.0 - rerank-multilingual-v2.0
column (str, default: "text" ) – The name of the column to use as input to the cross encoder model.
top_n (str, default: None ) – The number of results to return. If None, will return all results.
model_name
¶

model_name = model_name
column
¶

column = column
top_n
¶

top_n = top_n
api_key
¶

api_key = api_key
rerank_hybrid
¶

rerank_hybrid(query: str, vector_results: Table, fts_results: Table)
rerank_vector
¶

rerank_vector(query: str, vector_results: Table)
rerank_fts
¶

rerank_fts(query: str, fts_results: Table)
lancedb.rerankers.colbert.ColbertReranker
¶
Bases: AnswerdotaiRerankers

Reranks the results using the ColBERT model.

Parameters:

model_name (str, default: "colbert" (colbert-ir/colbert-v2.0) ) – The name of the cross encoder model to use.
column (str, default: "text" ) – The name of the column to use as input to the cross encoder model.
return_score (str, default: "relevance" ) – options are "relevance" or "all". Only "relevance" is supported for now.
**kwargs – Additional keyword arguments to pass to the model, for example, 'device'. See AnswerDotAI/rerankers for more information.
lancedb.rerankers.cross_encoder.CrossEncoderReranker
¶
Bases: Reranker

Reranks the results using a cross encoder model. The cross encoder model is used to score the query and each result. The results are then sorted by the score.

Parameters:

model_name (str, default: "cross-encoder/ms-marco-TinyBERT-L-6" ) – The name of the cross encoder model to use. See the sentence transformers documentation for a list of available models.
column (str, default: "text" ) – The name of the column to use as input to the cross encoder model.
device (str, default: None ) – The device to use for the cross encoder model. If None, will use "cuda" if available, otherwise "cpu".
return_score (str, default: "relevance" ) – options are "relevance" or "all". Only "relevance" is supported for now.
trust_remote_code (bool, default: True ) – If True, will trust the remote code to be safe. If False, will not trust the remote code and will not run it
model_name
¶

model_name = model_name
column
¶

column = column
device
¶

device = device
trust_remote_code
¶

trust_remote_code = trust_remote_code
model
¶

model
rerank_hybrid
¶

rerank_hybrid(query: str, vector_results: Table, fts_results: Table)
rerank_vector
¶

rerank_vector(query: str, vector_results: Table)
rerank_fts
¶

rerank_fts(query: str, fts_results: Table)
lancedb.rerankers.openai.OpenaiReranker
¶
Bases: Reranker

Reranks the results using the OpenAI API. WARNING: This is a prompt based reranker that uses chat model that is not a dedicated reranker API. This should be treated as experimental.

Parameters:

model_name (str, default: "gpt-4-turbo-preview" ) – The name of the cross encoder model to use.
column (str, default: "text" ) – The name of the column to use as input to the cross encoder model.
return_score (str, default: "relevance" ) – options are "relevance" or "all". Only "relevance" is supported for now.
api_key (str, default: None ) – The API key to use. If None, will use the OPENAI_API_KEY environment variable.
model_name
¶

model_name = model_name
column
¶

column = column
api_key
¶

api_key = api_key
rerank_hybrid
¶

rerank_hybrid(query: str, vector_results: Table, fts_results: Table)
rerank_vector
¶

rerank_vector(query: str, vector_results: Table)
rerank_fts
¶

rerank_fts(query: str, fts_results: Table)
Connections (Asynchronous)
¶
Connections represent a connection to a LanceDb database and can be used to create, list, or open tables.

lancedb.connect_async
¶

connect_async(uri: URI, *, api_key: Optional[str] = None, region: str = 'us-east-1', host_override: Optional[str] = None, read_consistency_interval: Optional[timedelta] = None, client_config: Optional[Union[ClientConfig, Dict[str, Any]]] = None, storage_options: Optional[Dict[str, str]] = None, session: Optional[Session] = None, manifest_enabled: bool = False, namespace_client_properties: Optional[Dict[str, str]] = None, oauth_config=None) -> AsyncConnection
Connect to a LanceDB database.

Parameters:

uri (URI) – The uri of the database.
api_key (Optional[str], default: None ) – If present, connect to LanceDB cloud. Otherwise, connect to a database on file system or cloud storage. Can be set via environment variable LANCEDB_API_KEY.
region (str, default: 'us-east-1' ) – The region to use for LanceDB Cloud.
host_override (Optional[str], default: None ) – The override url for LanceDB Cloud.
read_consistency_interval (Optional[timedelta], default: None ) – The interval at which to check for updates to the table from other processes. If None, then consistency is not checked. For performance reasons, this is the default. For strong consistency, set this to zero seconds. Then every read will check for updates from other processes. As a compromise, you can set this to a non-zero timedelta for eventual consistency. If more than that interval has passed since the last check, then the table will be checked for updates. Note: this consistency only applies to read operations. Write operations are always consistent.
Stronger consistency is not free. The smaller the interval, the more often each read pays the cost of checking for updates against object storage, raising per-read latency and cost.

client_config (Optional[Union[ClientConfig, Dict[str, Any]]], default: None ) – Configuration options for the LanceDB Cloud HTTP client. If a dict, then the keys are the attributes of the ClientConfig class. If None, then the default configuration is used.
storage_options (Optional[Dict[str, str]], default: None ) – Additional options for the storage backend. See available options at https://docs.lancedb.com/storage/
session (Optional[Session], default: None ) – (For LanceDB OSS only) A session to use for this connection. Sessions allow you to configure cache sizes for index and metadata caches, which can significantly impact memory use and performance. They can also be re-used across multiple connections to share the same cache state.
manifest_enabled (bool, default: False ) – When true for local/native connections, use directory namespace manifests as the source of truth for table metadata. Existing directory-listed root tables are migrated into the manifest on access.
namespace_client_properties (dict, default: None ) – Additional directory namespace client properties to use with manifest_enabled=True.
oauth_config (OAuthConfig, default: None ) – OAuth configuration for LanceDB Cloud/Enterprise. This is supported by connect_async only; synchronous connect uses API key authentication for db:// URIs.
Examples:


>>> import lancedb
>>> async def doctest_example():
...     # For a local directory, provide a path to the database
...     db = await lancedb.connect_async("~/.lancedb")
...     # For object storage, use a URI prefix
...     db = await lancedb.connect_async("s3://my-bucket/lancedb",
...                                      storage_options={
...                                          "aws_access_key_id": "***"})
...     # For tests and temporary data, use an in-memory database
...     db = await lancedb.connect_async("memory://")
...     # Connect to LanceDB cloud
...     db = await lancedb.connect_async("db://my_database", api_key="ldb_...",
...                                      client_config={
...                                          "retry_config": {"retries": 5}})
Returns:

conn ( AsyncConnection ) – A connection to a LanceDB database.
lancedb.db.AsyncConnection
¶
Bases: object

An active LanceDB connection

To obtain a connection you can use the connect_async function.

This could be a native connection (using lance) or a remote connection (e.g. for connecting to LanceDb Cloud)

Local connections do not currently hold any open resources but they may do so in the future (for example, for shared cache or connections to catalog services) Remote connections represent an open connection to the remote server. The close method can be used to release any underlying resources eagerly. The connection can also be used as a context manager.

Connections can be shared on multiple threads and are expected to be long lived. Connections can also be used as a context manager, however, in many cases a single connection can be used for the lifetime of the application and so this is often not needed. Closing a connection is optional. If it is not closed then it will be automatically closed when the connection object is deleted.

Examples:


>>> import lancedb
>>> async def doctest_example():
...   with await lancedb.connect_async("/tmp/my_dataset") as conn:
...     # do something with the connection
...     pass
...   # conn is closed here
uri
¶

uri: str
is_open
¶

is_open()
Return True if the connection is open.

close
¶

close()
Close the connection, releasing any underlying resources.

It is safe to call this method multiple times.

Any attempt to use the connection after it is closed will result in an error.

get_read_consistency_interval
¶

get_read_consistency_interval() -> Optional[timedelta]
list_namespaces
¶

list_namespaces(namespace_path: Optional[List[str]] = None, page_token: Optional[str] = None, limit: Optional[int] = None) -> ListNamespacesResponse
List immediate child namespace names in the given namespace.

Parameters:

namespace_path (Optional[List[str]], default: None ) – The parent namespace to list namespaces in. None or empty list represents root namespace.
page_token (Optional[str], default: None ) – The token to use for pagination. If not present, start from the beginning.
limit (Optional[int], default: None ) – The maximum number of results to return.
Returns:

ListNamespacesResponse – Response containing namespace names and optional pagination token
create_namespace
¶

create_namespace(namespace_path: List[str], mode: Optional[str] = None, properties: Optional[Dict[str, str]] = None) -> CreateNamespaceResponse
Create a new namespace.

Parameters:

namespace_path (List[str]) – The namespace identifier to create.
mode (Optional[str], default: None ) – Creation mode - "create", "exist_ok", or "overwrite". Case insensitive.
properties (Optional[Dict[str, str]], default: None ) – Properties to associate with the namespace
Returns:

CreateNamespaceResponse – Response containing namespace properties
drop_namespace
¶

drop_namespace(namespace_path: List[str], mode: Optional[str] = None, behavior: Optional[str] = None) -> DropNamespaceResponse
Drop a namespace.

Parameters:

namespace_path (List[str]) – The namespace identifier to drop.
mode (Optional[str], default: None ) – Whether to skip if not exists ("SKIP") or fail ("FAIL"). Case insensitive.
behavior (Optional[str], default: None ) – Whether to restrict drop if not empty ("RESTRICT") or cascade ("CASCADE"). Case insensitive.
Returns:

DropNamespaceResponse – Response containing properties and transaction_id if applicable.
describe_namespace
¶

describe_namespace(namespace_path: List[str]) -> DescribeNamespaceResponse
Describe a namespace.

Parameters:

namespace_path (List[str]) – The namespace identifier to describe.
Returns:

DescribeNamespaceResponse – Response containing the namespace properties.
list_tables
¶

list_tables(namespace_path: Optional[List[str]] = None, page_token: Optional[str] = None, limit: Optional[int] = None) -> ListTablesResponse
List all tables in this database with pagination support.

Parameters:

namespace_path (Optional[List[str]], default: None ) – The namespace to list tables in. None or empty list represents root namespace.
page_token (Optional[str], default: None ) – Token for pagination. Use the token from a previous response to get the next page of results.
limit (Optional[int], default: None ) – The maximum number of results to return.
Returns:

ListTablesResponse – Response containing table names and optional page_token for pagination.
table_names
¶

table_names(*, namespace_path: Optional[List[str]] = None, start_after: Optional[str] = None, limit: Optional[int] = None) -> Iterable[str]
List all tables in this database, in sorted order

.. deprecated:: Use :meth:list_tables instead, which provides proper pagination support.

Parameters:

namespace_path (Optional[List[str]], default: None ) – The namespace to list tables in. None or empty list represents root namespace.
start_after (Optional[str], default: None ) – If present, only return names that come lexicographically after the supplied value.
This can be combined with limit to implement pagination by setting this to the last table name from the previous page.

limit (Optional[int], default: None ) – The number of results to return.
Returns:

Iterable of str –
create_table
¶

create_table(name: str, data: Optional[DATA] = None, schema: Optional[Union[Schema, LanceModel]] = None, mode: Optional[Literal['create', 'overwrite']] = None, exist_ok: Optional[bool] = None, on_bad_vectors: Optional[str] = None, fill_value: Optional[float] = None, storage_options: Optional[Dict[str, str]] = None, *, namespace_path: Optional[List[str]] = None, embedding_functions: Optional[List[EmbeddingFunctionConfig]] = None, location: Optional[str] = None, namespace_client: Optional[Any] = None) -> AsyncTable
Create an AsyncTable in the database.

Parameters:

name (str) – The name of the table.
namespace_path (Optional[List[str]], default: None ) – The namespace to create the table in. Empty list represents root namespace.
data (Optional[DATA], default: None ) – User must provide at least one of data or schema. Acceptable types are:
list-of-dict

pandas.DataFrame

pyarrow.Table or pyarrow.RecordBatch

schema (Optional[Union[Schema, LanceModel]], default: None ) – Acceptable types are:
pyarrow.Schema

LanceModel

mode (Optional[Literal['create', 'overwrite']], default: None ) – The mode to use when creating the table. Can be either "create" or "overwrite". By default, if the table already exists, an exception is raised. If you want to overwrite the table, use mode="overwrite".
exist_ok (Optional[bool], default: None ) – If a table by the same name already exists, then raise an exception if exist_ok=False. If exist_ok=True, then open the existing table; it will not add the provided data but will validate against any schema that's specified.
on_bad_vectors (Optional[str], default: None ) – What to do if any of the vectors are not the same size or contains NaNs. One of "error", "drop", "fill".
fill_value (Optional[float], default: None ) – The value to use when filling vectors. Only used if on_bad_vectors="fill".
storage_options (Optional[Dict[str, str]], default: None ) – Additional options for the storage backend. Options already set on the connection will be inherited by the table, but can be overridden here. See available options at https://docs.lancedb.com/storage/
To enable stable row IDs (row IDs remain stable after compaction, update, delete, and merges), set new_table_enable_stable_row_ids to "true" in storage_options when connecting to the database.

Returns:

AsyncTable – A reference to the newly created table.
!!! note – The vector index won't be created by default. To create the index, call the create_index method on the table.
Examples:

Can create with list of tuples or dictionaries:


>>> import lancedb
>>> async def doctest_example():
...     db = await lancedb.connect_async("./.lancedb")
...     data = [{"vector": [1.1, 1.2], "lat": 45.5, "long": -122.7},
...             {"vector": [0.2, 1.8], "lat": 40.1, "long":  -74.1}]
...     my_table = await db.create_table("my_table", data)
...     print(await my_table.query().limit(5).to_arrow())
>>> import asyncio
>>> asyncio.run(doctest_example())
pyarrow.Table
vector: fixed_size_list<item: float>[2]
  child 0, item: float
lat: double
long: double
----
vector: [[[1.1,1.2],[0.2,1.8]]]
lat: [[45.5,40.1]]
long: [[-122.7,-74.1]]
You can also pass a pandas DataFrame:


>>> import pandas as pd
>>> data = pd.DataFrame({
...    "vector": [[1.1, 1.2], [0.2, 1.8]],
...    "lat": [45.5, 40.1],
...    "long": [-122.7, -74.1]
... })
>>> async def pandas_example():
...     db = await lancedb.connect_async("./.lancedb")
...     my_table = await db.create_table("table2", data)
...     print(await my_table.query().limit(5).to_arrow())
>>> asyncio.run(pandas_example())
pyarrow.Table
vector: fixed_size_list<item: float>[2]
  child 0, item: float
lat: double
long: double
----
vector: [[[1.1,1.2],[0.2,1.8]]]
lat: [[45.5,40.1]]
long: [[-122.7,-74.1]]
Data is converted to Arrow before being written to disk. For maximum control over how data is saved, either provide the PyArrow schema to convert to or else provide a PyArrow Table directly.


>>> import pyarrow as pa
>>> custom_schema = pa.schema([
...   pa.field("vector", pa.list_(pa.float32(), 2)),
...   pa.field("lat", pa.float32()),
...   pa.field("long", pa.float32())
... ])
>>> async def with_schema():
...     db = await lancedb.connect_async("./.lancedb")
...     my_table = await db.create_table("table3", data, schema = custom_schema)
...     print(await my_table.query().limit(5).to_arrow())
>>> asyncio.run(with_schema())
pyarrow.Table
vector: fixed_size_list<item: float>[2]
  child 0, item: float
lat: float
long: float
----
vector: [[[1.1,1.2],[0.2,1.8]]]
lat: [[45.5,40.1]]
long: [[-122.7,-74.1]]
It is also possible to create an table from [Iterable[pa.RecordBatch]]:


>>> import pyarrow as pa
>>> def make_batches():
...     for i in range(5):
...         yield pa.RecordBatch.from_arrays(
...             [
...                 pa.array([[3.1, 4.1], [5.9, 26.5]],
...                     pa.list_(pa.float32(), 2)),
...                 pa.array(["foo", "bar"]),
...                 pa.array([10.0, 20.0]),
...             ],
...             ["vector", "item", "price"],
...         )
>>> schema=pa.schema([
...     pa.field("vector", pa.list_(pa.float32(), 2)),
...     pa.field("item", pa.utf8()),
...     pa.field("price", pa.float32()),
... ])
>>> async def iterable_example():
...     db = await lancedb.connect_async("./.lancedb")
...     await db.create_table("table4", make_batches(), schema=schema)
>>> asyncio.run(iterable_example())
open_table
¶

open_table(name: str, *, namespace_path: Optional[List[str]] = None, storage_options: Optional[Dict[str, str]] = None, index_cache_size: Optional[int] = None, location: Optional[str] = None, namespace_client: Optional[Any] = None, managed_versioning: Optional[bool] = None, branch: Optional[str] = None, version: Optional[int] = None) -> AsyncTable
Open a Lance Table in the database.

Parameters:

name (str) – The name of the table.
namespace_path (Optional[List[str]], default: None ) – The namespace to open the table from. None or empty list represents root namespace.
storage_options (Optional[Dict[str, str]], default: None ) – Additional options for the storage backend. Options already set on the connection will be inherited by the table, but can be overridden here. See available options at https://docs.lancedb.com/storage/
index_cache_size (Optional[int], default: None ) – Deprecated: Use session-level cache configuration instead. Create a Session with custom cache sizes and pass it to lancedb.connect().
Set the size of the index cache, specified as a number of entries

The exact meaning of an "entry" will depend on the type of index: * IVF - there is one entry for each IVF partition * BTREE - there is one entry for the entire index

This cache applies to the entire opened table, across all indices. Setting this value higher will increase performance on larger datasets at the expense of more RAM

location (Optional[str], default: None ) – The explicit location (URI) of the table. If provided, the table will be opened from this location instead of deriving it from the database URI and table name.
managed_versioning (Optional[bool], default: None ) – Whether managed versioning is enabled for this table. If provided, avoids a redundant describe_table call when namespace_client is set.
branch (Optional[str], default: None ) – If provided, open a handle scoped to this branch instead of the default branch. Reads and writes operate in the branch's context.
version (Optional[int], default: None ) – If provided, open the table pinned to this version, producing a read-only handle. Composes with branch: when both are given, opens that branch at the version; otherwise opens main at the version. Call checkout_latest to return to a writable state.
Returns:

A LanceTable object representing the table. –
clone_table
¶

clone_table(target_table_name: str, source_uri: str, *, target_namespace_path: Optional[List[str]] = None, source_version: Optional[int] = None, source_tag: Optional[str] = None, is_shallow: bool = True) -> AsyncTable
Clone a table from a source table.

A shallow clone creates a new table that shares the underlying data files with the source table but has its own independent manifest. This allows both the source and cloned tables to evolve independently while initially sharing the same data, deletion, and index files.

Parameters:

target_table_name (str) – The name of the target table to create.
source_uri (str) – The URI of the source table to clone from.
target_namespace_path (Optional[List[str]], default: None ) – The namespace for the target table. None or empty list represents root namespace.
source_version (Optional[int], default: None ) – The version of the source table to clone.
source_tag (Optional[str], default: None ) – The tag of the source table to clone.
is_shallow (bool, default: True ) – Whether to perform a shallow clone (True) or deep clone (False). Currently only shallow clone is supported.
Returns:

An AsyncTable object representing the cloned table. –
rename_table
¶

rename_table(cur_name: str, new_name: str, cur_namespace_path: Optional[List[str]] = None, new_namespace_path: Optional[List[str]] = None)
Rename a table in the database.

Parameters:

cur_name (str) – The current name of the table.
new_name (str) – The new name of the table.
cur_namespace_path (Optional[List[str]], default: None ) – The namespace of the current table. None or empty list represents root namespace.
new_namespace_path (Optional[List[str]], default: None ) – The namespace to move the table to. If not specified, defaults to the same as cur_namespace.
drop_table
¶

drop_table(name: str, *, namespace_path: Optional[List[str]] = None, ignore_missing: bool = False)
Drop a table from the database.

Parameters:

name (str) – The name of the table.
namespace_path (Optional[List[str]], default: None ) – The namespace to drop the table from. Empty list represents root namespace.
ignore_missing (bool, default: False ) – If True, ignore if the table does not exist.
drop_all_tables
¶

drop_all_tables(namespace_path: Optional[List[str]] = None)
Drop all tables from the database.

Parameters:

namespace_path (Optional[List[str]], default: None ) – The namespace to drop all tables from. None or empty list represents root namespace.
namespace_client
¶

namespace_client() -> LanceNamespace
Get the equivalent namespace client for this connection.

For native storage connections, this returns a DirectoryNamespace pointing to the same root with the same storage options.

For namespace connections, this returns the backing namespace client.

For enterprise (remote) connections, this returns a RestNamespace with the same URI and authentication headers.

Returns:

LanceNamespace – The namespace client for this connection.
drop_database
¶

drop_database()
Drop database This is the same thing as dropping all the tables

Tables (Asynchronous)
¶
Table hold your actual data as a collection of records / rows.

lancedb.table.AsyncTable
¶
An AsyncTable is a collection of Records in a LanceDB Database.

An AsyncTable can be obtained from the AsyncConnection.create_table and AsyncConnection.open_table methods.

An AsyncTable object is expected to be long lived and reused for multiple operations. AsyncTable objects will cache a certain amount of index data in memory. This cache will be freed when the Table is garbage collected. To eagerly free the cache you can call the close method. Once the AsyncTable is closed, it cannot be used for any further operations.

An AsyncTable can also be used as a context manager, and will automatically close when the context is exited. Closing a table is optional. If you do not close the table, it will be closed when the AsyncTable object is garbage collected.

Examples:

Create using AsyncConnection.create_table (more examples in that method's documentation).


>>> import lancedb
>>> async def create_a_table():
...     db = await lancedb.connect_async("./.lancedb")
...     data = [{"vector": [1.1, 1.2], "b": 2}]
...     table = await db.create_table("my_table", data=data)
...     print(await table.query().limit(5).to_arrow())
>>> import asyncio
>>> asyncio.run(create_a_table())
pyarrow.Table
vector: fixed_size_list<item: float>[2]
  child 0, item: float
b: int64
----
vector: [[[1.1,1.2]]]
b: [[2]]
Can append new data with AsyncTable.add().


>>> async def add_to_table():
...     db = await lancedb.connect_async("./.lancedb")
...     table = await db.open_table("my_table")
...     await table.add([{"vector": [0.5, 1.3], "b": 4}])
>>> asyncio.run(add_to_table())
Can query the table with AsyncTable.vector_search.


>>> async def search_table_for_vector():
...     db = await lancedb.connect_async("./.lancedb")
...     table = await db.open_table("my_table")
...     results = (
...       await table.vector_search([0.4, 0.4]).select(["b", "vector"]).to_pandas()
...     )
...     print(results)
>>> asyncio.run(search_table_for_vector())
   b      vector  _distance
0  4  [0.5, 1.3]       0.82
1  2  [1.1, 1.2]       1.13
Search queries are much faster when an index is created. See AsyncTable.create_index.

name
¶

name: str
The name of the table.

tags
¶

tags: AsyncTags
Tag management for the dataset.

Similar to Git, tags are a way to add metadata to a specific version of the dataset.

.. warning::


Tagged versions are exempted from the
:py:meth:`optimize(cleanup_older_than)` process.

To remove a version that has been tagged, you must first
:py:meth:`~Tags.delete` the associated tag.
branches
¶

branches: AsyncBranches
Branch management for the table.

Branches are isolated, writable lines of history forked from another branch (or version). Writes on a branch do not affect main.

is_open
¶

is_open() -> bool
Return True if the table is open.

close
¶

close()
Close the table and free any resources associated with it.

It is safe to call this method multiple times.

Any attempt to use the table after it has been closed will raise an error.

set_unenforced_primary_key
¶

set_unenforced_primary_key(columns: Union[str, Iterable[str]]) -> None
Set the unenforced primary key for this table to the given ordered list of columns.

"Unenforced" means LanceDB does not check uniqueness on writes; the columns are recorded in the schema as the primary key so that features such as merge_insert can use them. Calling this again replaces any previously-set primary key.

Parameters:

columns (str or Iterable[str]) – Either a single column name (single-column key) or an ordered iterable of column names (composite key). Each column dtype must be one of: int32, int64, utf8, large_utf8, binary, large_binary, fixed_size_binary.
set_lsm_write_spec
¶

set_lsm_write_spec(spec: 'LsmWriteSpec') -> None
Install an LsmWriteSpec on this table.

The spec selects Lance's MemWAL LSM-style write path for future merge_insert calls. LsmWriteSpec chooses one of three sharding strategies:

LsmWriteSpec.bucket(column, num_buckets) — hash-bucket writes by the single-column unenforced primary key.
LsmWriteSpec.identity(column) — shard by the raw value of a scalar column.
LsmWriteSpec.unsharded() — route every write to a single shard.
All variants require the table to have an unenforced primary key set via [set_unenforced_primary_key]; bucket sharding additionally requires it to be the single column being bucketed.

Parameters:

spec (LsmWriteSpec) – The sharding spec to install.
Examples:


>>> from lancedb._lancedb import LsmWriteSpec
>>> # table.set_unenforced_primary_key("id")
>>> # table.set_lsm_write_spec(LsmWriteSpec.bucket("id", 16))
unset_lsm_write_spec
¶

unset_lsm_write_spec() -> None
Remove the LsmWriteSpec from this table.

Reverts to the standard merge_insert write path. Errors if no spec is currently set.

close_lsm_writers
¶

close_lsm_writers() -> None
Drain and close any cached MemWAL shard writers for this table.

When an LSM write spec is installed, merge_insert opens MemWAL shard writers and caches them for reuse across calls. This closes them, flushing pending data; writers reopen lazily on the next merge_insert. It is a no-op when no writers are cached.

schema
¶

schema() -> Schema
The Arrow Schema of this Table

embedding_functions
¶

embedding_functions() -> Dict[str, EmbeddingFunctionConfig]
Get the embedding functions for the table

Returns:

funcs ( Dict[str, EmbeddingFunctionConfig] ) – A mapping of the vector column to the embedding function or empty dict if not configured.
count_rows
¶

count_rows(filter: Optional[str] = None) -> int
Count the number of rows in the table.

Parameters:

filter (Optional[str], default: None ) – A SQL where clause to filter the rows to count.
head
¶

head(n=5) -> Table
Return the first n rows of the table.

Parameters:

n – The number of rows to return.
query
¶

query() -> AsyncQuery
Returns an AsyncQuery that can be used to search the table.

Use methods on the returned query to control query behavior. The query can be executed with methods like to_arrow, to_pandas and more.

to_pandas
¶

to_pandas(blob_mode: BlobMode = 'lazy', **kwargs) -> 'pd.DataFrame'
Return the table as a pandas DataFrame.

Parameters:

blob_mode (BlobMode, default: 'lazy' ) – Controls how Lance blob columns are returned.
**kwargs – Forwarded to PyArrow / Lance pandas conversion.
Returns:

DataFrame –
to_arrow
¶

to_arrow() -> Table
Return the table as a pyarrow Table.

Returns:

Table –
create_index
¶

create_index(column: str, *, replace: Optional[bool] = None, config: Optional[Union[IvfFlat, IvfPq, IvfRq, HnswPq, HnswSq, HnswFlat, BTree, Bitmap, LabelList, Fm, FTS]] = None, wait_timeout: Optional[timedelta] = None, name: Optional[str] = None, train: bool = True)
Create an index to speed up queries

Indices can be created on vector columns or scalar columns. Indices on vector columns will speed up vector searches. Indices on scalar columns will speed up filtering (in both vector and non-vector searches)

Parameters:

column (str) – The column to index.
replace (Optional[bool], default: None ) – Whether to replace the existing index
If this is false, and another index already exists on the same columns and the same name, then an error will be returned. This is true even if that index is out of date.

The default is True

config (Optional[Union[IvfFlat, IvfPq, IvfRq, HnswPq, HnswSq, HnswFlat, BTree, Bitmap, LabelList, Fm, FTS]], default: None ) – For advanced configuration you can specify the type of index you would like to create. You can also specify index-specific parameters when creating an index object.
wait_timeout (Optional[timedelta], default: None ) – The timeout to wait if indexing is asynchronous.
name (Optional[str], default: None ) – The name of the index. If not provided, a default name will be generated.
train (bool, default: True ) – Whether to train the index with existing data. Vector indices always train with existing data.
drop_index
¶

drop_index(name: str) -> None
Drop an index from the table.

Parameters:

name (str) – The name of the index to drop.
Notes
This does not delete the index from disk, it just removes it from the table. To delete the index, run optimize after dropping the index.

Use list_indices to find the names of the indices.

prewarm_index
¶

prewarm_index(name: str) -> None
Prewarm an index in the table.

This is a hint to the database that the index will be accessed in the future and should be loaded into memory if possible. This can reduce cold-start latency for subsequent queries.

This call initiates prewarming and returns once the request is accepted. It is idempotent and safe to call from multiple clients concurrently.

It is generally wasteful to call this if the index does not fit into the available cache. Not all index types support prewarming; unsupported indices will silently ignore the request.

Parameters:

name (str) – The name of the index to prewarm
prewarm_data
¶

prewarm_data(columns: Optional[List[str]] = None) -> None
Prewarm data for the table.

This is a hint to the database that the given columns will be accessed in the future and the database should prefetch the data if possible. Currently only supported on remote tables.

This call initiates prewarming and returns once the request is accepted. It is idempotent and safe to call from multiple clients concurrently.

This operation has a large upfront cost but can speed up future queries that need to fetch the given columns. Large columns such as embeddings or binary data may not be practical to prewarm. This feature is intended for workloads that issue many queries against the same columns.

Parameters:

columns (Optional[List[str]], default: None ) – The columns to prewarm. If None, all columns are prewarmed.
wait_for_index
¶

wait_for_index(index_names: Iterable[str], timeout: timedelta = timedelta(seconds=300)) -> None
Wait for indexing to complete for the given index names. This will poll the table until all the indices are fully indexed, or raise a timeout exception if the timeout is reached.

Parameters:

index_names (Iterable[str]) – The name of the indices to poll
timeout (timedelta, default: timedelta(seconds=300) ) – Timeout to wait for asynchronous indexing. The default is 5 minutes.
stats
¶

stats() -> TableStatistics
Retrieve table and fragment statistics.

uri
¶

uri() -> str
Get the table URI (storage location).

For remote tables, this fetches the location from the server via describe. For local tables, this returns the dataset URI.

Returns:

str – The full storage location of the table (e.g., S3/GCS path).
initial_storage_options
¶

initial_storage_options() -> Optional[Dict[str, str]]
Get the initial storage options that were passed in when opening this table.

For dynamically refreshed options (e.g., credential vending), use :meth:latest_storage_options.

Warning: This is an internal API and the return value is subject to change.

Returns:

Optional[Dict[str, str]] – The storage options, or None if no storage options were configured.
latest_storage_options
¶

latest_storage_options() -> Optional[Dict[str, str]]
Get the latest storage options, refreshing from provider if configured.

This method is useful for credential vending scenarios where storage options may be refreshed dynamically. If no dynamic provider is configured, this returns the initial static options.

Warning: This is an internal API and the return value is subject to change.

Returns:

Optional[Dict[str, str]] – The storage options, or None if no storage options were configured.
add
¶

add(data: DATA, *, mode: Optional[Literal['append', 'overwrite']] = 'append', on_bad_vectors: Optional[OnBadVectorsType] = None, fill_value: Optional[float] = None, progress: Optional[Union[bool, Callable, Any]] = None) -> AddResult
Add more data to the Table.

Parameters:

data (DATA) – The data to insert into the table. Acceptable types are:
list-of-dict

pandas.DataFrame

pyarrow.Table or pyarrow.RecordBatch

mode (Optional[Literal['append', 'overwrite']], default: 'append' ) – The mode to use when writing the data. Valid values are "append" and "overwrite".
on_bad_vectors (Optional[OnBadVectorsType], default: None ) – What to do if any of the vectors are not the same size or contains NaNs. One of "error", "drop", "fill", "null".
fill_value (Optional[float], default: None ) – The value to use when filling vectors. Only used if on_bad_vectors="fill".
progress (Optional[Union[bool, Callable, Any]], default: None ) – A callback or tqdm-compatible progress bar. See :meth:Table.add for details.
merge_insert
¶

merge_insert(on: Union[str, Iterable[str]]) -> LanceMergeInsertBuilder
Returns a LanceMergeInsertBuilder that can be used to create a "merge insert" operation

This operation can add rows, update rows, and remove rows all in a single transaction. It is a very generic tool that can be used to create behaviors like "insert if not exists", "update or insert (i.e. upsert)", or even replace a portion of existing data with new data (e.g. replace all data where month="january")

The merge insert operation works by combining new data from a source table with existing data in a target table by using a join. There are three categories of records.

"Matched" records are records that exist in both the source table and the target table. "Not matched" records exist only in the source table (e.g. these are new data) "Not matched by source" records exist only in the target table (this is old data)

The builder returned by this method can be used to customize what should happen for each category of data.

Please note that the data may appear to be reordered as part of this operation. This is because updated rows will be deleted from the dataset and then reinserted at the end with the new values.

Parameters:

on (Union[str, Iterable[str]]) – A column (or columns) to join on. This is how records from the source table and target table are matched. Typically this is some kind of key or id column.
Examples:


>>> import lancedb
>>> data = pa.table({"a": [2, 1, 3], "b": ["a", "b", "c"]})
>>> db = lancedb.connect("./.lancedb")
>>> table = db.create_table("my_table", data)
>>> new_data = pa.table({"a": [2, 3, 4], "b": ["x", "y", "z"]})
>>> # Perform a "upsert" operation
>>> res = table.merge_insert("a")     \
...      .when_matched_update_all()     \
...      .when_not_matched_insert_all() \
...      .execute(new_data)
>>> res
MergeResult(version=2, num_updated_rows=2, num_inserted_rows=1, num_deleted_rows=0, num_attempts=1, num_rows=3)
>>> # The order of new rows is non-deterministic since we use
>>> # a hash-join as part of this operation and so we sort here
>>> table.to_arrow().sort_by("a").to_pandas()
   a  b
0  1  b
1  2  x
2  3  y
3  4  z
search
¶

search(query: Optional[Union[VEC, str, 'PIL.Image.Image', Tuple, FullTextQuery]] = None, vector_column_name: Optional[str] = None, query_type: QueryType = 'auto', ordering_field_name: Optional[str] = None, fts_columns: Optional[Union[str, List[str]]] = None) -> Union[AsyncHybridQuery, AsyncFTSQuery, AsyncVectorQuery]
Create a search query to find the nearest neighbors of the given query vector. We currently support vector search and [full-text search][experimental-full-text-search].

All query options are defined in AsyncQuery.

Parameters:

query (Optional[Union[VEC, str, 'PIL.Image.Image', Tuple, FullTextQuery]], default: None ) – The targetted vector to search for.
default None. Acceptable types are: list, np.ndarray, PIL.Image.Image

If None then the select/where/limit clauses are applied to filter the table

vector_column_name (Optional[str], default: None ) – The name of the vector column to search.
The vector column needs to be a pyarrow fixed size list type

If not specified then the vector column is inferred from the table schema

If the table has multiple vector columns then the vector_column_name needs to be specified. Otherwise, an error is raised.

query_type (QueryType, default: 'auto' ) – default "auto". Acceptable types are: "vector", "fts", "hybrid", or "auto"
If "auto" then the query type is inferred from the query;

If query is a list/np.ndarray then the query type is "vector";

If query is a PIL.Image.Image then either do vector search, or raise an error if no corresponding embedding function is found.

If query is a string, then the query type is "vector" if the table has embedding functions else the query type is "fts"

Returns:

LanceQueryBuilder – A query builder object representing the query.
vector_search
¶

vector_search(query_vector: Union[VEC, Tuple]) -> AsyncVectorQuery
Search the table with a given query vector. This is a convenience method for preparing a vector query and is the same thing as calling nearestTo on the builder returned by query. Seer nearest_to for more details.

delete
¶

delete(where: Union[str, Expr]) -> DeleteResult
Delete rows from the table.

This can be used to delete a single row, many rows, all rows, or sometimes no rows (if your predicate matches nothing).

Parameters:

where (Union[str, Expr]) – The filter condition. Can be a SQL string or a type-safe :class:~lancedb.expr.Expr built with :func:~lancedb.expr.col and :func:~lancedb.expr.lit.
The filter must not be empty, or it will error.

Examples:


>>> import lancedb
>>> data = [
...    {"x": 1, "vector": [1.0, 2]},
...    {"x": 2, "vector": [3.0, 4]},
...    {"x": 3, "vector": [5.0, 6]}
... ]
>>> db = lancedb.connect("./.lancedb")
>>> table = db.create_table("my_table", data)
>>> table.to_pandas()
   x      vector
0  1  [1.0, 2.0]
1  2  [3.0, 4.0]
2  3  [5.0, 6.0]
>>> table.delete("x = 2")
DeleteResult(num_deleted_rows=1, version=2)
>>> table.to_pandas()
   x      vector
0  1  [1.0, 2.0]
1  3  [5.0, 6.0]
If you have a list of values to delete, you can combine them into a stringified list and use the IN operator:


>>> to_remove = [1, 5]
>>> to_remove = ", ".join([str(v) for v in to_remove])
>>> to_remove
'1, 5'
>>> table.delete(f"x IN ({to_remove})")
DeleteResult(num_deleted_rows=1, version=3)
>>> table.to_pandas()
   x      vector
0  3  [5.0, 6.0]
update
¶

update(updates: Optional[Dict[str, Any]] = None, *, where: Optional[str] = None, updates_sql: Optional[Dict[str, str]] = None) -> UpdateResult
This can be used to update zero to all rows in the table.

If a filter is provided with where then only rows matching the filter will be updated. Otherwise all rows will be updated.

Parameters:

updates (Optional[Dict[str, Any]], default: None ) – The updates to apply. The keys should be the name of the column to update. The values should be the new values to assign. This is required unless updates_sql is supplied.
where (Optional[str], default: None ) – An SQL filter that controls which rows are updated. For example, 'x = 2' or 'x IN (1, 2, 3)'. Only rows that satisfy this filter will be udpated.
updates_sql (Optional[Dict[str, str]], default: None ) – The updates to apply, expressed as SQL expression strings. The keys should be column names. The values should be SQL expressions. These can be SQL literals (e.g. "7" or "'foo'") or they can be expressions based on the previous value of the row (e.g. "x + 1" to increment the x column by 1)
Returns:

UpdateResult – An object containing: - rows_updated: The number of rows that were updated - version: The new version number of the table after the update
Examples:


>>> import asyncio
>>> import lancedb
>>> import pandas as pd
>>> async def demo_update():
...     data = pd.DataFrame({"x": [1, 2], "vector": [[1, 2], [3, 4]]})
...     db = await lancedb.connect_async("./.lancedb")
...     table = await db.create_table("my_table", data)
...     # x is [1, 2], vector is [[1, 2], [3, 4]]
...     await table.update({"vector": [10, 10]}, where="x = 2")
...     # x is [1, 2], vector is [[1, 2], [10, 10]]
...     await table.update(updates_sql={"x": "x + 1"})
...     # x is [2, 3], vector is [[1, 2], [10, 10]]
>>> asyncio.run(demo_update())
add_columns
¶

add_columns(transforms: dict[str, str] | field | List[field] | Schema) -> AddColumnsResult
Add new columns with defined values.

Parameters:

transforms (dict[str, str] | field | List[field] | Schema) – A map of column name to a SQL expression to use to calculate the value of the new column. These expressions will be evaluated for each row in the table, and can reference existing columns. Alternatively, you can pass a pyarrow field or schema to add new columns with NULLs.
Returns:

AddColumnsResult – version: the new version number of the table after adding columns.
alter_columns
¶

alter_columns(*alterations: Iterable[dict[str, Any]]) -> AlterColumnsResult
Alter column names and nullability.

alterations : Iterable[Dict[str, Any]] A sequence of dictionaries, each with the following keys: - "path": str The column path to alter. For a top-level column, this is the name. For a nested column, this is the dot-separated path, e.g. "a.b.c". - "rename": str, optional The new name of the column. If not specified, the column name is not changed. - "data_type": pyarrow.DataType, optional The new data type of the column. Existing values will be casted to this type. If not specified, the column data type is not changed. - "nullable": bool, optional Whether the column should be nullable. If not specified, the column nullability is not changed. Only non-nullable columns can be changed to nullable. Currently, you cannot change a nullable column to non-nullable.

Returns:

AlterColumnsResult – version: the new version number of the table after the alteration.
update_field_metadata
¶

update_field_metadata(*updates: dict[str, Any]) -> UpdateFieldMetadataResult
Update per-field metadata. See Table.update_field_metadata.

drop_columns
¶

drop_columns(columns: Iterable[str])
Drop columns from the table.

Parameters:

columns (Iterable[str]) – The names of the columns to drop.
version
¶

version() -> int
Retrieve the version of the table

LanceDb supports versioning. Every operation that modifies the table increases version. As long as a version hasn't been deleted you can [Self::checkout] that version to view the data at that point. In addition, you can [Self::restore] the version to replace the current table with a previous version.

list_versions
¶

list_versions()
List all versions of the table

checkout
¶

checkout(version: int | str)
Checks out a specific version of the Table

Any read operation on the table will now access the data at the checked out version. As a consequence, calling this method will disable any read consistency interval that was previously set.

This is a read-only operation that turns the table into a sort of "view" or "detached head". Other table instances will not be affected. To make the change permanent you can use the [Self::restore] method.

Any operation that modifies the table will fail while the table is in a checked out state.

Parameters:

version (int | str) – The version to check out. A version number (int) or a tag (str) can be provided.
To –
checkout_latest
¶

checkout_latest()
Ensures the table is pointing at the latest version

This can be used to manually update a table when the read_consistency_interval is None It can also be used to undo a [Self::checkout] operation

restore
¶

restore(version: Optional[int | str] = None)
Restore the table to the currently checked out version

This operation will fail if checkout has not been called previously

This operation will overwrite the latest version of the table with a previous version. Any changes made since the checked out version will no longer be visible.

Once the operation concludes the table will no longer be in a checked out state and the read_consistency_interval, if any, will apply.

take_offsets
¶

take_offsets(offsets: list[int]) -> AsyncTakeQuery
Take a list of offsets from the table.

Offsets are 0-indexed and relative to the current version of the table. Offsets are not stable. A row with an offset of N may have a different offset in a different version of the table (e.g. if an earlier row is deleted).

Offsets are mostly useful for sampling as the set of all valid offsets is easily known in advance to be [0, len(table)).

Parameters:

offsets (list[int]) – The offsets to take.
Returns:

RecordBatch – A record batch containing the rows at the given offsets.
take_row_ids
¶

take_row_ids(row_ids: list[int]) -> AsyncTakeQuery
Take a list of row ids from the table.

Row ids are not stable and are relative to the current version of the table. They can change due to compaction and updates.

Unlike offsets, row ids are not 0-indexed and no assumptions should be made about the possible range of row ids. In order to use this method you must first obtain the row ids by scanning or searching the table.

Even so, row ids are more stable than offsets and can be useful in some situations.

There is an ongoing effort to make row ids stable which is tracked at https://github.com/lancedb/lancedb/issues/1120

Parameters:

row_ids (list[int]) – The row ids to take.
Returns:

AsyncTakeQuery – A query object that can be executed to get the rows.
current_branch
¶

current_branch() -> Optional[str]
The branch this table handle is scoped to, or None for main.

optimize
¶

optimize(*, cleanup_older_than: Optional[timedelta] = None, delete_unverified: bool = False, retrain=False) -> OptimizeStats
Optimize the on-disk data and indices for better performance.

Modeled after VACUUM in PostgreSQL.

Optimization covers three operations:

Compaction: Merges small files into larger ones
Prune: Removes old versions of the dataset
Index: Optimizes the indices, adding new data to existing indices
Parameters:

cleanup_older_than (Optional[timedelta], default: None ) – All files belonging to versions older than this will be removed. Set to 0 days to remove all versions except the latest. The latest version is never removed.
delete_unverified (bool, default: False ) – Files leftover from a failed transaction may appear to be part of an in-progress operation (e.g. appending new data) and these files will not be deleted unless they are at least 7 days old. If delete_unverified is True then these files will be deleted regardless of their age.
.. warning::


This should only be set to True if you can guarantee that no other
process is currently working on this dataset. Otherwise the dataset
could be put into a corrupted state.
retrain – This parameter is no longer used and is deprecated.
The –
data –
optimize –
you –
modification –
list_indices
¶

list_indices() -> Iterable[IndexConfig]
List all indices that have been created with Self::create_index

index_stats
¶

index_stats(index_name: str) -> Optional[IndexStatistics]
Retrieve statistics about an index

Parameters:

index_name (str) – The name of the index to retrieve statistics for
Returns:

IndexStatistics or None – The statistics about the index. Returns None if the index does not exist.
uses_v2_manifest_paths
¶

uses_v2_manifest_paths() -> bool
Check if the table is using the new v2 manifest paths.

Returns:

bool – True if the table is using the new v2 manifest paths, False otherwise.
migrate_manifest_paths_v2
¶

migrate_manifest_paths_v2()
Migrate the manifest paths to the new format.

This will update the manifest to use the new v2 format for paths.

This function is idempotent, and can be run multiple times without changing the state of the object store.

Danger

This should not be run while other concurrent operations are happening. And it should also run until completion before resuming other operations.

You can use AsyncTable.uses_v2_manifest_paths to check if the table is already using the new path style.

replace_field_metadata
¶

replace_field_metadata(field_name: str, new_metadata: dict[str, str])
Replace the metadata of a field in the schema

.. deprecated:: 0.33.1 Use :func:update_field_metadata instead.

Parameters:

field_name (str) – The name of the field to replace the metadata for
new_metadata (dict[str, str]) – The new metadata to set
lancedb.table.AsyncTags
¶
Async table tag manager.

list
¶

list() -> Dict[str, Tag]
List all table tags.

Returns:

dict[str, Tag] – A dictionary mapping tag names to version numbers.
get_version
¶

get_version(tag: str) -> int
Get the version of a tag.

Parameters:

tag (str) – The name of the tag to get the version for.
create
¶

create(tag: str, version: int) -> None
Create a tag for a given table version.

Parameters:

tag (str) – The name of the tag to create. This name must be unique among all tag names for the table.
version (int) – The table version to tag.
delete
¶

delete(tag: str) -> None
Delete tag from the table.

Parameters:

tag (str) – The name of the tag to delete.
update
¶

update(tag: str, version: int) -> None
Update tag to a new version.

Parameters:

tag (str) – The name of the tag to update.
version (int) – The new table version to tag.
Indices (Asynchronous)
¶
Indices can be created on a table to speed up queries. This section lists the indices that LanceDb supports.

lancedb.index.BTree
¶
Describes a btree index configuration

A btree index is an index on scalar columns. The index stores a copy of the column in sorted order. A header entry is created for each block of rows (currently the block size is fixed at 4096). These header entries are stored in a separate cacheable structure (a btree). To search for data the header is used to determine which blocks need to be read from disk.

For example, a btree index in a table with 1Bi rows requires sizeof(Scalar) * 256Ki bytes of memory and will generally need to read sizeof(Scalar) * 4096 bytes to find the correct row ids.

This index is good for scalar columns with mostly distinct values and does best when the query is highly selective. It works with numeric, temporal, and string columns.

The btree index does not currently have any parameters though parameters such as the block size may be added in the future.

lancedb.index.Bitmap
¶
Describe a Bitmap index configuration.

A Bitmap index stores a bitmap for each distinct value in the column for every row.

This index works best for low-cardinality numeric or string columns, where the number of unique values is small (i.e., less than a few thousands). Bitmap index can accelerate the following filters:

<, <=, =, >, >=
IN (value1, value2, ...)
between (value1, value2)
is null
For example, a bitmap index with a table with 1Bi rows, and 128 distinct values, requires 128 / 8 * 1Bi bytes on disk.

lancedb.index.LabelList
¶
Describe a LabelList index configuration.

LabelList is a scalar index that can be used on List<T> columns to support queries with array_contains_all and array_contains_any using an underlying bitmap index.

For example, it works with tags, categories, keywords, etc.

lancedb.index.FTS
¶
Describe a FTS index configuration.

FTS is a full-text search index that can be used on String columns

For example, it works with title, description, content, etc.

Notes
Model-backed tokenizers such as jieba/default and lindera/ipadic require tokenizer models in Lance's language model home. Set LANCE_LANGUAGE_MODEL_HOME to override the default platform data directory under lance/language_models.

with_position
¶

with_position: bool = False
base_tokenizer
¶

base_tokenizer: BaseTokenizerType = 'simple'
language
¶

language: str = 'English'
max_token_length
¶

max_token_length: Optional[int] = 40
lower_case
¶

lower_case: bool = True
stem
¶

stem: bool = True
remove_stop_words
¶

remove_stop_words: bool = True
ascii_folding
¶

ascii_folding: bool = True
ngram_min_length
¶

ngram_min_length: int = 3
ngram_max_length
¶

ngram_max_length: int = 3
prefix_only
¶

prefix_only: bool = False
lancedb.index.IvfPq
¶
Describes an IVF PQ Index

This index stores a compressed (quantized) copy of every vector. These vectors are grouped into partitions of similar vectors. Each partition keeps track of a centroid which is the average value of all vectors in the group.

During a query the centroids are compared with the query vector to find the closest partitions. The compressed vectors in these partitions are then searched to find the closest vectors.

The compression scheme is called product quantization. Each vector is divide into subvectors and then each subvector is quantized into a small number of bits. the parameters num_bits and num_subvectors control this process, providing a tradeoff between index size (and thus search speed) and index accuracy.

The partitioning process is called IVF and the num_partitions parameter controls how many groups to create.

Note that training an IVF PQ index on a large dataset is a slow operation and currently is also a memory intensive operation.

distance_type
¶

distance_type: Literal['l2', 'cosine', 'dot'] = 'l2'
num_partitions
¶

num_partitions: Optional[int] = None
num_sub_vectors
¶

num_sub_vectors: Optional[int] = None
num_bits
¶

num_bits: int = 8
max_iterations
¶

max_iterations: int = 50
sample_rate
¶

sample_rate: int = 256
target_partition_size
¶

target_partition_size: Optional[int] = None
accelerator
¶

accelerator: Optional[str] = None
lancedb.index.HnswPq
¶
Describe a HNSW-PQ index configuration.

HNSW-PQ stands for Hierarchical Navigable Small World - Product Quantization. It is a variant of the HNSW algorithm that uses product quantization to compress the vectors. To create an HNSW-PQ index, you can specify the following parameters:

Parameters:

distance_type (Literal['l2', 'cosine', 'dot'], default: 'l2' ) – The distance metric used to train the index.
The following distance types are available:

"l2" - Euclidean distance. This is a very common distance metric that accounts for both magnitude and direction when determining the distance between vectors. l2 distance has a range of [0, ∞).

"cosine" - Cosine distance. Cosine distance is a distance metric calculated from the cosine similarity between two vectors. Cosine similarity is a measure of similarity between two non-zero vectors of an inner product space. It is defined to equal the cosine of the angle between them. Unlike l2, the cosine distance is not affected by the magnitude of the vectors. Cosine distance has a range of [0, 2].

"dot" - Dot product. Dot distance is the dot product of two vectors. Dot distance has a range of (-∞, ∞). If the vectors are normalized (i.e. their l2 norm is 1), then dot distance is equivalent to the cosine distance.

num_partitions (Optional[int], default: None ) – The number of IVF partitions to create.
For HNSW, we recommend a small number of partitions. Setting this to 1 works well for most tables. For very large tables, training just one HNSW graph will require too much memory. Each partition becomes its own HNSW graph, so setting this value higher reduces the peak memory use of training.

default (Optional[int], default: None ) – The number of IVF partitions to create.
For HNSW, we recommend a small number of partitions. Setting this to 1 works well for most tables. For very large tables, training just one HNSW graph will require too much memory. Each partition becomes its own HNSW graph, so setting this value higher reduces the peak memory use of training.

num_sub_vectors (Optional[int], default: None ) – Number of sub-vectors of PQ.
This value controls how much the vector is compressed during the quantization step. The more sub vectors there are the less the vector is compressed. The default is the dimension of the vector divided by 16. If the dimension is not evenly divisible by 16 we use the dimension divided by 8.

The above two cases are highly preferred. Having 8 or 16 values per subvector allows us to use efficient SIMD instructions.

If the dimension is not visible by 8 then we use 1 subvector. This is not ideal and will likely result in poor performance.

num_bits: int, default 8 Number of bits to encode each sub-vector.

This value controls how much the sub-vectors are compressed. The more bits the more accurate the index but the slower search. Only 4 and 8 are supported.

default (Optional[int], default: None ) – Number of sub-vectors of PQ.
This value controls how much the vector is compressed during the quantization step. The more sub vectors there are the less the vector is compressed. The default is the dimension of the vector divided by 16. If the dimension is not evenly divisible by 16 we use the dimension divided by 8.

The above two cases are highly preferred. Having 8 or 16 values per subvector allows us to use efficient SIMD instructions.

If the dimension is not visible by 8 then we use 1 subvector. This is not ideal and will likely result in poor performance.

num_bits: int, default 8 Number of bits to encode each sub-vector.

This value controls how much the sub-vectors are compressed. The more bits the more accurate the index but the slower search. Only 4 and 8 are supported.

max_iterations (int, default: 50 ) – Max iterations to train kmeans.
When training an IVF index we use kmeans to calculate the partitions. This parameter controls how many iterations of kmeans to run.

Increasing this might improve the quality of the index but in most cases the parameter is unused because kmeans will converge with fewer iterations. The parameter is only used in cases where kmeans does not appear to converge. In those cases it is unlikely that setting this larger will lead to the index converging anyways.

default (int, default: 50 ) – Max iterations to train kmeans.
When training an IVF index we use kmeans to calculate the partitions. This parameter controls how many iterations of kmeans to run.

Increasing this might improve the quality of the index but in most cases the parameter is unused because kmeans will converge with fewer iterations. The parameter is only used in cases where kmeans does not appear to converge. In those cases it is unlikely that setting this larger will lead to the index converging anyways.

sample_rate (int, default: 256 ) – The rate used to calculate the number of training vectors for kmeans.
When an IVF index is trained, we need to calculate partitions. These are groups of vectors that are similar to each other. To do this we use an algorithm called kmeans.

Running kmeans on a large dataset can be slow. To speed this up we run kmeans on a random sample of the data. This parameter controls the size of the sample. The total number of vectors used to train the index is sample_rate * num_partitions.

Increasing this value might improve the quality of the index but in most cases the default should be sufficient.

default (int, default: 256 ) – The rate used to calculate the number of training vectors for kmeans.
When an IVF index is trained, we need to calculate partitions. These are groups of vectors that are similar to each other. To do this we use an algorithm called kmeans.

Running kmeans on a large dataset can be slow. To speed this up we run kmeans on a random sample of the data. This parameter controls the size of the sample. The total number of vectors used to train the index is sample_rate * num_partitions.

Increasing this value might improve the quality of the index but in most cases the default should be sufficient.

m (int, default: 20 ) – The number of neighbors to select for each vector in the HNSW graph.
This value controls the tradeoff between search speed and accuracy. The higher the value the more accurate the search but the slower it will be.

default (int, default: 20 ) – The number of neighbors to select for each vector in the HNSW graph.
This value controls the tradeoff between search speed and accuracy. The higher the value the more accurate the search but the slower it will be.

ef_construction (int, default: 300 ) – The number of candidates to evaluate during the construction of the HNSW graph.
This value controls the tradeoff between build speed and accuracy. The higher the value the more accurate the build but the slower it will be. 150 to 300 is the typical range. 100 is a minimum for good quality search results. In most cases, there is no benefit to setting this higher than 500. This value should be set to a value that is not less than ef in the search phase.

default (int, default: 300 ) – The number of candidates to evaluate during the construction of the HNSW graph.
This value controls the tradeoff between build speed and accuracy. The higher the value the more accurate the build but the slower it will be. 150 to 300 is the typical range. 100 is a minimum for good quality search results. In most cases, there is no benefit to setting this higher than 500. This value should be set to a value that is not less than ef in the search phase.

target_partition_size (Optional[int], default: None ) – The target size of each partition.
This value controls the tradeoff between search performance and accuracy. faster search but less accurate results as higher value.

default (Optional[int], default: None ) – The target size of each partition.
This value controls the tradeoff between search performance and accuracy. faster search but less accurate results as higher value.

distance_type
¶

distance_type: Literal['l2', 'cosine', 'dot'] = 'l2'
num_partitions
¶

num_partitions: Optional[int] = None
num_sub_vectors
¶

num_sub_vectors: Optional[int] = None
num_bits
¶

num_bits: int = 8
max_iterations
¶

max_iterations: int = 50
sample_rate
¶

sample_rate: int = 256
m
¶

m: int = 20
ef_construction
¶

ef_construction: int = 300
target_partition_size
¶

target_partition_size: Optional[int] = None
accelerator
¶

accelerator: Optional[str] = None
lancedb.index.HnswSq
¶
Describe a HNSW-SQ index configuration.

HNSW-SQ stands for Hierarchical Navigable Small World - Scalar Quantization. It is a variant of the HNSW algorithm that uses scalar quantization to compress the vectors.

Parameters:

distance_type (Literal['l2', 'cosine', 'dot'], default: 'l2' ) – The distance metric used to train the index.
The following distance types are available:

"l2" - Euclidean distance. This is a very common distance metric that accounts for both magnitude and direction when determining the distance between vectors. l2 distance has a range of [0, ∞).

"cosine" - Cosine distance. Cosine distance is a distance metric calculated from the cosine similarity between two vectors. Cosine similarity is a measure of similarity between two non-zero vectors of an inner product space. It is defined to equal the cosine of the angle between them. Unlike l2, the cosine distance is not affected by the magnitude of the vectors. Cosine distance has a range of [0, 2].

"dot" - Dot product. Dot distance is the dot product of two vectors. Dot distance has a range of (-∞, ∞). If the vectors are normalized (i.e. their l2 norm is 1), then dot distance is equivalent to the cosine distance.

num_partitions (Optional[int], default: None ) – The number of IVF partitions to create.
For HNSW, we recommend a small number of partitions. Setting this to 1 works well for most tables. For very large tables, training just one HNSW graph will require too much memory. Each partition becomes its own HNSW graph, so setting this value higher reduces the peak memory use of training.

default (Optional[int], default: None ) – The number of IVF partitions to create.
For HNSW, we recommend a small number of partitions. Setting this to 1 works well for most tables. For very large tables, training just one HNSW graph will require too much memory. Each partition becomes its own HNSW graph, so setting this value higher reduces the peak memory use of training.

max_iterations (int, default: 50 ) – Max iterations to train kmeans.
When training an IVF index we use kmeans to calculate the partitions. This parameter controls how many iterations of kmeans to run.

Increasing this might improve the quality of the index but in most cases the parameter is unused because kmeans will converge with fewer iterations. The parameter is only used in cases where kmeans does not appear to converge. In those cases it is unlikely that setting this larger will lead to the index converging anyways.

default (int, default: 50 ) – Max iterations to train kmeans.
When training an IVF index we use kmeans to calculate the partitions. This parameter controls how many iterations of kmeans to run.

Increasing this might improve the quality of the index but in most cases the parameter is unused because kmeans will converge with fewer iterations. The parameter is only used in cases where kmeans does not appear to converge. In those cases it is unlikely that setting this larger will lead to the index converging anyways.

sample_rate (int, default: 256 ) – The rate used to calculate the number of training vectors for kmeans.
When an IVF index is trained, we need to calculate partitions. These are groups of vectors that are similar to each other. To do this we use an algorithm called kmeans.

Running kmeans on a large dataset can be slow. To speed this up we run kmeans on a random sample of the data. This parameter controls the size of the sample. The total number of vectors used to train the index is sample_rate * num_partitions.

Increasing this value might improve the quality of the index but in most cases the default should be sufficient.

default (int, default: 256 ) – The rate used to calculate the number of training vectors for kmeans.
When an IVF index is trained, we need to calculate partitions. These are groups of vectors that are similar to each other. To do this we use an algorithm called kmeans.

Running kmeans on a large dataset can be slow. To speed this up we run kmeans on a random sample of the data. This parameter controls the size of the sample. The total number of vectors used to train the index is sample_rate * num_partitions.

Increasing this value might improve the quality of the index but in most cases the default should be sufficient.

m (int, default: 20 ) – The number of neighbors to select for each vector in the HNSW graph.
This value controls the tradeoff between search speed and accuracy. The higher the value the more accurate the search but the slower it will be.

default (int, default: 20 ) – The number of neighbors to select for each vector in the HNSW graph.
This value controls the tradeoff between search speed and accuracy. The higher the value the more accurate the search but the slower it will be.

ef_construction (int, default: 300 ) – The number of candidates to evaluate during the construction of the HNSW graph.
This value controls the tradeoff between build speed and accuracy. The higher the value the more accurate the build but the slower it will be. 150 to 300 is the typical range. 100 is a minimum for good quality search results. In most cases, there is no benefit to setting this higher than 500. This value should be set to a value that is not less than ef in the search phase.

default (int, default: 300 ) – The number of candidates to evaluate during the construction of the HNSW graph.
This value controls the tradeoff between build speed and accuracy. The higher the value the more accurate the build but the slower it will be. 150 to 300 is the typical range. 100 is a minimum for good quality search results. In most cases, there is no benefit to setting this higher than 500. This value should be set to a value that is not less than ef in the search phase.

target_partition_size (Optional[int], default: None ) – The target size of each partition.
This value controls the tradeoff between search performance and accuracy. faster search but less accurate results as higher value.

default (Optional[int], default: None ) – The target size of each partition.
This value controls the tradeoff between search performance and accuracy. faster search but less accurate results as higher value.

distance_type
¶

distance_type: Literal['l2', 'cosine', 'dot'] = 'l2'
num_partitions
¶

num_partitions: Optional[int] = None
max_iterations
¶

max_iterations: int = 50
sample_rate
¶

sample_rate: int = 256
m
¶

m: int = 20
ef_construction
¶

ef_construction: int = 300
target_partition_size
¶

target_partition_size: Optional[int] = None
accelerator
¶

accelerator: Optional[str] = None
lancedb.index.IvfFlat
¶
Describes an IVF Flat Index

This index stores raw vectors. These vectors are grouped into partitions of similar vectors. Each partition keeps track of a centroid which is the average value of all vectors in the group.

distance_type
¶

distance_type: Literal['l2', 'cosine', 'dot', 'hamming'] = 'l2'
num_partitions
¶

num_partitions: Optional[int] = None
max_iterations
¶

max_iterations: int = 50
sample_rate
¶

sample_rate: int = 256
target_partition_size
¶

target_partition_size: Optional[int] = None
accelerator
¶

accelerator: Optional[str] = None
lancedb.index.IvfSq
¶
Describes an IVF Scalar Quantization (SQ) index.

This index applies scalar quantization to compress vectors and organizes the quantized vectors into IVF partitions. It offers a balance between search speed and storage efficiency while keeping good recall.

distance_type
¶

distance_type: Literal['l2', 'cosine', 'dot'] = 'l2'
num_partitions
¶

num_partitions: Optional[int] = None
max_iterations
¶

max_iterations: int = 50
sample_rate
¶

sample_rate: int = 256
target_partition_size
¶

target_partition_size: Optional[int] = None
accelerator
¶

accelerator: Optional[str] = None
lancedb.index.IvfRq
¶
Describes an IVF RQ Index

IVF-RQ (RabitQ Quantization) compresses vectors using RabitQ quantization and organizes them into IVF partitions.

The compression scheme is called RabitQ quantization. Each dimension is quantized into a small number of bits. The parameters num_bits and num_partitions control this process, providing a tradeoff between index size (and thus search speed) and index accuracy.

The partitioning process is called IVF and the num_partitions parameter controls how many groups to create.

Note that training an IVF RQ index on a large dataset is a slow operation and currently is also a memory intensive operation.

distance_type
¶

distance_type: Literal['l2', 'cosine', 'dot'] = 'l2'
num_partitions
¶

num_partitions: Optional[int] = None
num_bits
¶

num_bits: int = 1
max_iterations
¶

max_iterations: int = 50
sample_rate
¶

sample_rate: int = 256
target_partition_size
¶

target_partition_size: Optional[int] = None
accelerator
¶

accelerator: Optional[str] = None
lancedb.index.HnswFlat
¶
Describe a HNSW-FLAT index configuration.

HNSW-FLAT stands for Hierarchical Navigable Small World without quantization. It stores raw vectors in the HNSW graph, providing the highest recall among the IVF_HNSW family at the cost of more memory and disk space compared to :class:HnswSq or :class:HnswPq.

Parameters:

distance_type (Literal['l2', 'cosine', 'dot'], default: 'l2' ) – The distance metric used to train the index.
The following distance types are available:

"l2" - Euclidean distance. This is a very common distance metric that accounts for both magnitude and direction when determining the distance between vectors. l2 distance has a range of [0, ∞).

"cosine" - Cosine distance. Cosine distance is a distance metric calculated from the cosine similarity between two vectors. Cosine similarity is a measure of similarity between two non-zero vectors of an inner product space. It is defined to equal the cosine of the angle between them. Unlike l2, the cosine distance is not affected by the magnitude of the vectors. Cosine distance has a range of [0, 2].

"dot" - Dot product. Dot distance is the dot product of two vectors. Dot distance has a range of (-∞, ∞). If the vectors are normalized (i.e. their l2 norm is 1), then dot distance is equivalent to the cosine distance.

num_partitions (Optional[int], default: None ) – The number of IVF partitions to create.
For HNSW, we recommend a small number of partitions. Setting this to 1 works well for most tables. For very large tables, training just one HNSW graph will require too much memory. Each partition becomes its own HNSW graph, so setting this value higher reduces the peak memory use of training.

default (Optional[int], default: None ) – The number of IVF partitions to create.
For HNSW, we recommend a small number of partitions. Setting this to 1 works well for most tables. For very large tables, training just one HNSW graph will require too much memory. Each partition becomes its own HNSW graph, so setting this value higher reduces the peak memory use of training.

max_iterations (int, default: 50 ) – Max iterations to train kmeans.
When training an IVF index we use kmeans to calculate the partitions. This parameter controls how many iterations of kmeans to run.

default (int, default: 50 ) – Max iterations to train kmeans.
When training an IVF index we use kmeans to calculate the partitions. This parameter controls how many iterations of kmeans to run.

sample_rate (int, default: 256 ) – The rate used to calculate the number of training vectors for kmeans.
default (int, default: 256 ) – The rate used to calculate the number of training vectors for kmeans.
m (int, default: 20 ) – The number of neighbors to select for each vector in the HNSW graph.
This value controls the tradeoff between search speed and accuracy. The higher the value the more accurate the search but the slower it will be.

default (int, default: 20 ) – The number of neighbors to select for each vector in the HNSW graph.
This value controls the tradeoff between search speed and accuracy. The higher the value the more accurate the search but the slower it will be.

ef_construction (int, default: 300 ) – The number of candidates to evaluate during the construction of the HNSW graph.
This value controls the tradeoff between build speed and accuracy. The higher the value the more accurate the build but the slower it will be. 150 to 300 is the typical range. 100 is a minimum for good quality search results. In most cases, there is no benefit to setting this higher than 500. This value should be set to a value that is not less than ef in the search phase.

default (int, default: 300 ) – The number of candidates to evaluate during the construction of the HNSW graph.
This value controls the tradeoff between build speed and accuracy. The higher the value the more accurate the build but the slower it will be. 150 to 300 is the typical range. 100 is a minimum for good quality search results. In most cases, there is no benefit to setting this higher than 500. This value should be set to a value that is not less than ef in the search phase.

target_partition_size (Optional[int], default: None ) – The target size of each partition.
default (Optional[int], default: None ) – The target size of each partition.
distance_type
¶

distance_type: Literal['l2', 'cosine', 'dot'] = 'l2'
num_partitions
¶

num_partitions: Optional[int] = None
max_iterations
¶

max_iterations: int = 50
sample_rate
¶

sample_rate: int = 256
m
¶

m: int = 20
ef_construction
¶

ef_construction: int = 300
target_partition_size
¶

target_partition_size: Optional[int] = None
lancedb.table.IndexStatistics
¶
Statistics about an index.

num_indexed_rows
¶

num_indexed_rows: int
num_unindexed_rows
¶

num_unindexed_rows: int
index_type
¶

index_type: Literal['IVF_FLAT', 'IVF_SQ', 'IVF_PQ', 'IVF_RQ', 'IVF_HNSW_SQ', 'IVF_HNSW_PQ', 'IVF_HNSW_FLAT', 'FTS', 'BTREE', 'BITMAP', 'LABEL_LIST']
distance_type
¶

distance_type: Optional[Literal['l2', 'cosine', 'dot']] = None
num_indices
¶

num_indices: Optional[int] = None
Querying (Asynchronous)
¶
Queries allow you to return data from your database. Basic queries can be created with the AsyncTable.query method to return the entire (typically filtered) table. Vector searches return the rows nearest to a query vector and can be created with the AsyncTable.vector_search method.

lancedb.query.AsyncQuery
¶
Bases: AsyncStandardQuery

to_query_object
¶

to_query_object() -> Query
Convert the query into a query object

This is currently experimental but can be useful as the query object is pure python and more easily serializable.

select
¶

select(columns: Union[List[str], dict[str, str]]) -> Self
Return only the specified columns.

By default a query will return all columns from the table. However, this can have a very significant impact on latency. LanceDb stores data in a columnar fashion. This means we can finely tune our I/O to select exactly the columns we need.

As a best practice you should always limit queries to the columns that you need. If you pass in a list of column names then only those columns will be returned.

You can also use this method to create new "dynamic" columns based on your existing columns. For example, you may not care about "a" or "b" but instead simply want "a + b". This is often seen in the SELECT clause of an SQL query (e.g. SELECT a+b FROM my_table).

To create dynamic columns you can pass in a dict[str, str]. A column will be returned for each entry in the map. The key provides the name of the column. The value is an SQL string used to specify how the column is calculated.

For example, an SQL query might state SELECT a + b AS combined, c. The equivalent input to this method would be {"combined": "a + b", "c": "c"}.

Columns will always be returned in the order given, even if that order is different than the order used when adding the data.

with_row_id
¶

with_row_id() -> Self
Include the _rowid column in the results.

with_row_address
¶

with_row_address(with_row_address: bool = True) -> Self
Include the _rowaddr column in scanner-backed plain query results.

with_fragments
¶

with_fragments(fragments: Any) -> Self
Restrict scanner-backed plain query results to the given Lance fragments.

fragment_ids
¶

fragment_ids(fragment_ids: List[int]) -> Self
Restrict scanner-backed plain query results to the given Lance fragment ids.

to_batches
¶

to_batches(*, max_batch_length: Optional[int] = None, timeout: Optional[timedelta] = None) -> AsyncRecordBatchReader
Execute the query and return the results as an Apache Arrow RecordBatchReader.

Parameters:

max_batch_length (Optional[int], default: None ) – The maximum number of selected records in a single RecordBatch object. If not specified, a default batch length is used. It is possible for batches to be smaller than the provided length if the underlying data is stored in smaller chunks.
timeout (Optional[timedelta], default: None ) – The maximum time to wait for the query to complete. If not specified, no timeout is applied. If the query does not complete within the specified time, an error will be raised.
output_schema
¶

output_schema() -> Schema
Return the output schema for the query

This does not execute the query.

to_arrow
¶

to_arrow(timeout: Optional[timedelta] = None) -> Table
Execute the query and collect the results into an Apache Arrow Table.

This method will collect all results into memory before returning. If you expect a large number of results, you may want to use to_batches

Parameters:

timeout (Optional[timedelta], default: None ) – The maximum time to wait for the query to complete. If not specified, no timeout is applied. If the query does not complete within the specified time, an error will be raised.
to_list
¶

to_list(timeout: Optional[timedelta] = None) -> List[dict]
Execute the query and return the results as a list of dictionaries.

Each list entry is a dictionary with the selected column names as keys, or all table columns if select is not called. The vector and the "_distance" fields are returned whether or not they're explicitly selected.

Parameters:

timeout (Optional[timedelta], default: None ) – The maximum time to wait for the query to complete. If not specified, no timeout is applied. If the query does not complete within the specified time, an error will be raised.
to_pandas
¶

to_pandas(flatten: Optional[Union[int, bool]] = None, timeout: Optional[timedelta] = None, *, blob_mode: BlobMode = 'lazy', **kwargs) -> 'pd.DataFrame'
Execute the query and collect the results into a pandas DataFrame.

This method will collect all results into memory before returning. If you expect a large number of results, you may want to use to_batches and convert each batch to pandas separately.

Examples:


>>> import asyncio
>>> from lancedb import connect_async
>>> async def doctest_example():
...     conn = await connect_async("./.lancedb")
...     table = await conn.create_table("my_table", data=[{"a": 1, "b": 2}])
...     async for batch in await table.query().to_batches():
...         batch_df = batch.to_pandas()
>>> asyncio.run(doctest_example())
Parameters:

flatten (Optional[Union[int, bool]], default: None ) – If flatten is True, flatten all nested columns. If flatten is an integer, flatten the nested columns up to the specified depth. If unspecified, do not flatten the nested columns.
timeout (Optional[timedelta], default: None ) – The maximum time to wait for the query to complete. If not specified, no timeout is applied. If the query does not complete within the specified time, an error will be raised.
blob_mode (BlobMode, default: 'lazy' ) – Controls how blob columns are returned for plain scan queries. Vector, FTS, hybrid, and other non-native query shapes keep the existing Arrow conversion path and only support blob descriptions.
**kwargs – Forwarded to pyarrow.Table.to_pandas after query execution and optional flattening.
to_polars
¶

to_polars(timeout: Optional[timedelta] = None) -> 'pl.DataFrame'
Execute the query and collect the results into a Polars DataFrame.

This method will collect all results into memory before returning. If you expect a large number of results, you may want to use to_batches and convert each batch to polars separately.

Parameters:

timeout (Optional[timedelta], default: None ) – The maximum time to wait for the query to complete. If not specified, no timeout is applied. If the query does not complete within the specified time, an error will be raised.
Examples:


>>> import asyncio
>>> import polars as pl
>>> from lancedb import connect_async
>>> async def doctest_example():
...     conn = await connect_async("./.lancedb")
...     table = await conn.create_table("my_table", data=[{"a": 1, "b": 2}])
...     async for batch in await table.query().to_batches():
...         batch_df = pl.from_arrow(batch)
>>> asyncio.run(doctest_example())
to_pydantic
¶

to_pydantic(model: Type[LanceModel], *, timeout: Optional[timedelta] = None) -> List[LanceModel]
Convert results to a list of pydantic models.

Parameters:

model (Type[LanceModel]) – The pydantic model to use.
timeout (timedelta, default: None ) – The maximum time to wait for the query to complete. If None, wait indefinitely.
Returns:

list[LanceModel] –
explain_plan
¶

explain_plan(verbose: Optional[bool] = False)
Return the execution plan for this query.

Examples:


>>> import asyncio
>>> from lancedb import connect_async
>>> async def doctest_example():
...     conn = await connect_async("./.lancedb")
...     table = await conn.create_table("my_table", [{"vector": [99.0, 99.0]}])
...     plan = await table.query().nearest_to([1.0, 2.0]).explain_plan(True)
...     print(plan)
>>> asyncio.run(doctest_example())
ProjectionExec: expr=[vector@0 as vector, _distance@2 as _distance]
  GlobalLimitExec: skip=0, fetch=10
    FilterExec: _distance@2 IS NOT NULL
      SortExec: TopK(fetch=10), expr=[_distance@2 ASC NULLS LAST, _rowid@1 ASC NULLS LAST], preserve_partitioning=[false]
        KNNVectorDistance: metric=l2
          LanceRead: uri=..., projection=[vector], ...
Parameters:

verbose (bool, default: False ) – Use a verbose output format.
Returns:

plan ( str ) –
analyze_plan
¶

analyze_plan()
Execute the query and display with runtime metrics.

Returns:

plan ( str ) –
where
¶

where(predicate: Union[str, Expr]) -> Self
Only return rows matching the given predicate

The predicate can be a SQL string or a type-safe :class:~lancedb.expr.Expr built with :func:~lancedb.expr.col and :func:~lancedb.expr.lit.

Examples:


>>> predicate = "x > 10"
>>> predicate = "y > 0 AND y < 100"
>>> predicate = "x > 5 OR y = 'test'"
Filtering performance can often be improved by creating a scalar index on the filter column(s).

Calling this multiple times combines the filters with a logical AND rather than replacing the previous filter.

limit
¶

limit(limit: int) -> Self
Set the maximum number of results to return.

By default, a plain search has no limit. If this method is not called then every valid row from the table will be returned.

offset
¶

offset(offset: int) -> Self
Set the offset for the results.

Parameters:

offset (int) – The offset to start fetching results from.
order_by
¶

order_by(ordering: Optional[List[ColumnOrdering]]) -> Self
Set the ordering for the results.

Parameters:

ordering (Optional[List[ColumnOrdering]]) – The ordering to use for the results. If None, then the default ordering will be used.
fast_search
¶

fast_search() -> Self
Skip searching un-indexed data.

This can make queries faster, but will miss any data that has not been indexed.

Tip

You can add new data into an existing index by calling AsyncTable.optimize.

postfilter
¶

postfilter() -> Self
If this is called then filtering will happen after the search instead of before. By default filtering will be performed before the search. This is how filtering is typically understood to work. This prefilter step does add some additional latency. Creating a scalar index on the filter column(s) can often improve this latency. However, sometimes a filter is too complex or scalar indices cannot be applied to the column. In these cases postfiltering can be used instead of prefiltering to improve latency. Post filtering applies the filter to the results of the search. This means we only run the filter on a much smaller set of data. However, it can cause the query to return fewer than limit results (or even no results) if none of the nearest results match the filter. Post filtering happens during the "refine stage" (described in more detail in @see {@link VectorQuery#refineFactor}). This means that setting a higher refine factor can often help restore some of the results lost by post filtering.

nearest_to
¶

nearest_to(query_vector: Union[VEC, Tuple, List[VEC]]) -> AsyncVectorQuery
Find the nearest vectors to the given query vector.

This converts the query from a plain query to a vector query.

This method will attempt to convert the input to the query vector expected by the embedding model. If the input cannot be converted then an error will be thrown.

By default, there is no embedding model, and the input should be something that can be converted to a pyarrow array of floats. This includes lists, numpy arrays, and tuples.

If there is only one vector column (a column whose data type is a fixed size list of floats) then the column does not need to be specified. If there is more than one vector column you must use AsyncVectorQuery.column to specify which column you would like to compare with.

If no index has been created on the vector column then a vector query will perform a distance comparison between the query vector and every vector in the database and then sort the results. This is sometimes called a "flat search"

For small databases, with tens of thousands of vectors or less, this can be reasonably fast. In larger databases you should create a vector index on the column. If there is a vector index then an "approximate" nearest neighbor search (frequently called an ANN search) will be performed. This search is much faster, but the results will be approximate.

The query can be further parameterized using the returned builder. There are various ANN search parameters that will let you fine tune your recall accuracy vs search latency.

Vector searches always have a limit. If limit has not been called then a default limit of 10 will be used.

Typically, a single vector is passed in as the query. However, you can also pass in multiple vectors. When multiple vectors are passed in, if the vector column is with multivector type, then the vectors will be treated as a single query. Or the vectors will be treated as multiple queries, this can be useful if you want to find the nearest vectors to multiple query vectors. This is not expected to be faster than making multiple queries concurrently; it is just a convenience method. If multiple vectors are passed in then an additional column query_index will be added to the results. This column will contain the index of the query vector that the result is nearest to.

nearest_to_text
¶

nearest_to_text(query: str | FullTextQuery, columns: Union[str, List[str], None] = None) -> AsyncFTSQuery
Find the documents that are most relevant to the given text query.

This method will perform a full text search on the table and return the most relevant documents. The relevance is determined by BM25.

The columns to search must be with native FTS index (Tantivy-based can't work with this method).

By default, all indexed columns are searched, now only one column can be searched at a time.

Parameters:

query (str | FullTextQuery) – The text query to search for.
columns (Union[str, List[str], None], default: None ) – The columns to search in. If None, all indexed columns are searched. For now only one column can be searched at a time.
lancedb.query.AsyncVectorQuery
¶
Bases: AsyncStandardQuery, AsyncVectorQueryBase

column
¶

column(column: str) -> Self
Set the vector column to query

This controls which column is compared to the query vector supplied in the call to AsyncQuery.nearest_to.

This parameter must be specified if the table has more than one column whose data type is a fixed-size-list of floats.

nprobes
¶

nprobes(nprobes: int) -> Self
Set the number of partitions to search (probe)

This argument is only used when the vector column has an IVF-based index. If there is no index then this value is ignored.

The IVF stage of IVF PQ divides the input into partitions (clusters) of related values.

The partition whose centroids are closest to the query vector will be exhaustiely searched to find matches. This parameter controls how many partitions should be searched.

Increasing this value will increase the recall of your query but will also increase the latency of your query. The default value is 20. This default is good for many cases but the best value to use will depend on your data and the recall that you need to achieve.

For best results we recommend tuning this parameter with a benchmark against your actual data to find the smallest possible value that will still give you the desired recall.

minimum_nprobes
¶

minimum_nprobes(minimum_nprobes: int) -> Self
Set the minimum number of probes to use.

See nprobes for more details.

These partitions will be searched on every indexed vector query and will increase recall at the expense of latency.

maximum_nprobes
¶

maximum_nprobes(maximum_nprobes: int) -> Self
Set the maximum number of probes to use.

See nprobes for more details.

If this value is greater than minimum_nprobes then the excess partitions will be searched only if we have not found enough results.

This can be useful when there is a narrow filter to allow these queries to spend more time searching and avoid potential false negatives.

If this value is 0 then no limit will be applied and all partitions could be searched if needed to satisfy the limit.

distance_range
¶

distance_range(lower_bound: Optional[float] = None, upper_bound: Optional[float] = None) -> Self
Set the distance range to use.

Only rows with distances within range [lower_bound, upper_bound) will be returned.

Parameters:

lower_bound (Optional[float], default: None ) – The lower bound of the distance range.
upper_bound (Optional[float], default: None ) – The upper bound of the distance range.
Returns:

AsyncVectorQuery – The AsyncVectorQuery object.
ef
¶

ef(ef: int) -> Self
Set the number of candidates to consider during search

This argument is only used when the vector column has an HNSW index. If there is no index then this value is ignored.

Increasing this value will increase the recall of your query but will also increase the latency of your query. The default value is 1.5 * limit. This default is good for many cases but the best value to use will depend on your data and the recall that you need to achieve.

refine_factor
¶

refine_factor(refine_factor: int) -> Self
A multiplier to control how many additional rows are taken during the refine step

This argument is only used when the vector column has an IVF PQ index. If there is no index then this value is ignored.

An IVF PQ index stores compressed (quantized) values. They query vector is compared against these values and, since they are compressed, the comparison is inaccurate.

This parameter can be used to refine the results. It can improve both improve recall and correct the ordering of the nearest results.

To refine results LanceDb will first perform an ANN search to find the nearest limit * refine_factor results. In other words, if refine_factor is 3 and limit is the default (10) then the first 30 results will be selected. LanceDb then fetches the full, uncompressed, values for these 30 results. The results are then reordered by the true distance and only the nearest 10 are kept.

Note: there is a difference between calling this method with a value of 1 and never calling this method at all. Calling this method with any value will have an impact on your search latency. When you call this method with a refine_factor of 1 then LanceDb still needs to fetch the full, uncompressed, values so that it can potentially reorder the results.

Note: if this method is NOT called then the distances returned in the _distance column will be approximate distances based on the comparison of the quantized query vector and the quantized result vectors. This can be considerably different than the true distance between the query vector and the actual uncompressed vector.

distance_type
¶

distance_type(distance_type: str) -> Self
Set the distance metric to use

When performing a vector search we try and find the "nearest" vectors according to some kind of distance metric. This parameter controls which distance metric to use. See @see {@link IvfPqOptions.distanceType} for more details on the different distance metrics available.

Note: if there is a vector index then the distance type used MUST match the distance type used to train the vector index. If this is not done then the results will be invalid.

By default "l2" is used.

bypass_vector_index
¶

bypass_vector_index() -> Self
If this is called then any vector index is skipped

An exhaustive (flat) search will be performed. The query vector will be compared to every vector in the table. At high scales this can be expensive. However, this is often still useful. For example, skipping the vector index can give you ground truth results which you can use to calculate your recall to select an appropriate value for nprobes.

to_query_object
¶

to_query_object() -> Query
Convert the query into a query object

This is currently experimental but can be useful as the query object is pure python and more easily serializable.

select
¶

select(columns: Union[List[str], dict[str, str]]) -> Self
Return only the specified columns.

By default a query will return all columns from the table. However, this can have a very significant impact on latency. LanceDb stores data in a columnar fashion. This means we can finely tune our I/O to select exactly the columns we need.

As a best practice you should always limit queries to the columns that you need. If you pass in a list of column names then only those columns will be returned.

You can also use this method to create new "dynamic" columns based on your existing columns. For example, you may not care about "a" or "b" but instead simply want "a + b". This is often seen in the SELECT clause of an SQL query (e.g. SELECT a+b FROM my_table).

To create dynamic columns you can pass in a dict[str, str]. A column will be returned for each entry in the map. The key provides the name of the column. The value is an SQL string used to specify how the column is calculated.

For example, an SQL query might state SELECT a + b AS combined, c. The equivalent input to this method would be {"combined": "a + b", "c": "c"}.

Columns will always be returned in the order given, even if that order is different than the order used when adding the data.

with_row_id
¶

with_row_id() -> Self
Include the _rowid column in the results.

with_row_address
¶

with_row_address(with_row_address: bool = True) -> Self
Include the _rowaddr column in scanner-backed plain query results.

with_fragments
¶

with_fragments(fragments: Any) -> Self
Restrict scanner-backed plain query results to the given Lance fragments.

fragment_ids
¶

fragment_ids(fragment_ids: List[int]) -> Self
Restrict scanner-backed plain query results to the given Lance fragment ids.

output_schema
¶

output_schema() -> Schema
Return the output schema for the query

This does not execute the query.

to_arrow
¶

to_arrow(timeout: Optional[timedelta] = None) -> Table
Execute the query and collect the results into an Apache Arrow Table.

This method will collect all results into memory before returning. If you expect a large number of results, you may want to use to_batches

Parameters:

timeout (Optional[timedelta], default: None ) – The maximum time to wait for the query to complete. If not specified, no timeout is applied. If the query does not complete within the specified time, an error will be raised.
to_list
¶

to_list(timeout: Optional[timedelta] = None) -> List[dict]
Execute the query and return the results as a list of dictionaries.

Each list entry is a dictionary with the selected column names as keys, or all table columns if select is not called. The vector and the "_distance" fields are returned whether or not they're explicitly selected.

Parameters:

timeout (Optional[timedelta], default: None ) – The maximum time to wait for the query to complete. If not specified, no timeout is applied. If the query does not complete within the specified time, an error will be raised.
to_pandas
¶

to_pandas(flatten: Optional[Union[int, bool]] = None, timeout: Optional[timedelta] = None, *, blob_mode: BlobMode = 'lazy', **kwargs) -> 'pd.DataFrame'
Execute the query and collect the results into a pandas DataFrame.

This method will collect all results into memory before returning. If you expect a large number of results, you may want to use to_batches and convert each batch to pandas separately.

Examples:


>>> import asyncio
>>> from lancedb import connect_async
>>> async def doctest_example():
...     conn = await connect_async("./.lancedb")
...     table = await conn.create_table("my_table", data=[{"a": 1, "b": 2}])
...     async for batch in await table.query().to_batches():
...         batch_df = batch.to_pandas()
>>> asyncio.run(doctest_example())
Parameters:

flatten (Optional[Union[int, bool]], default: None ) – If flatten is True, flatten all nested columns. If flatten is an integer, flatten the nested columns up to the specified depth. If unspecified, do not flatten the nested columns.
timeout (Optional[timedelta], default: None ) – The maximum time to wait for the query to complete. If not specified, no timeout is applied. If the query does not complete within the specified time, an error will be raised.
blob_mode (BlobMode, default: 'lazy' ) – Controls how blob columns are returned for plain scan queries. Vector, FTS, hybrid, and other non-native query shapes keep the existing Arrow conversion path and only support blob descriptions.
**kwargs – Forwarded to pyarrow.Table.to_pandas after query execution and optional flattening.
to_polars
¶

to_polars(timeout: Optional[timedelta] = None) -> 'pl.DataFrame'
Execute the query and collect the results into a Polars DataFrame.

This method will collect all results into memory before returning. If you expect a large number of results, you may want to use to_batches and convert each batch to polars separately.

Parameters:

timeout (Optional[timedelta], default: None ) – The maximum time to wait for the query to complete. If not specified, no timeout is applied. If the query does not complete within the specified time, an error will be raised.
Examples:


>>> import asyncio
>>> import polars as pl
>>> from lancedb import connect_async
>>> async def doctest_example():
...     conn = await connect_async("./.lancedb")
...     table = await conn.create_table("my_table", data=[{"a": 1, "b": 2}])
...     async for batch in await table.query().to_batches():
...         batch_df = pl.from_arrow(batch)
>>> asyncio.run(doctest_example())
to_pydantic
¶

to_pydantic(model: Type[LanceModel], *, timeout: Optional[timedelta] = None) -> List[LanceModel]
Convert results to a list of pydantic models.

Parameters:

model (Type[LanceModel]) – The pydantic model to use.
timeout (timedelta, default: None ) – The maximum time to wait for the query to complete. If None, wait indefinitely.
Returns:

list[LanceModel] –
explain_plan
¶

explain_plan(verbose: Optional[bool] = False)
Return the execution plan for this query.

Examples:


>>> import asyncio
>>> from lancedb import connect_async
>>> async def doctest_example():
...     conn = await connect_async("./.lancedb")
...     table = await conn.create_table("my_table", [{"vector": [99.0, 99.0]}])
...     plan = await table.query().nearest_to([1.0, 2.0]).explain_plan(True)
...     print(plan)
>>> asyncio.run(doctest_example())
ProjectionExec: expr=[vector@0 as vector, _distance@2 as _distance]
  GlobalLimitExec: skip=0, fetch=10
    FilterExec: _distance@2 IS NOT NULL
      SortExec: TopK(fetch=10), expr=[_distance@2 ASC NULLS LAST, _rowid@1 ASC NULLS LAST], preserve_partitioning=[false]
        KNNVectorDistance: metric=l2
          LanceRead: uri=..., projection=[vector], ...
Parameters:

verbose (bool, default: False ) – Use a verbose output format.
Returns:

plan ( str ) –
analyze_plan
¶

analyze_plan()
Execute the query and display with runtime metrics.

Returns:

plan ( str ) –
where
¶

where(predicate: Union[str, Expr]) -> Self
Only return rows matching the given predicate

The predicate can be a SQL string or a type-safe :class:~lancedb.expr.Expr built with :func:~lancedb.expr.col and :func:~lancedb.expr.lit.

Examples:


>>> predicate = "x > 10"
>>> predicate = "y > 0 AND y < 100"
>>> predicate = "x > 5 OR y = 'test'"
Filtering performance can often be improved by creating a scalar index on the filter column(s).

Calling this multiple times combines the filters with a logical AND rather than replacing the previous filter.

limit
¶

limit(limit: int) -> Self
Set the maximum number of results to return.

By default, a plain search has no limit. If this method is not called then every valid row from the table will be returned.

offset
¶

offset(offset: int) -> Self
Set the offset for the results.

Parameters:

offset (int) – The offset to start fetching results from.
order_by
¶

order_by(ordering: Optional[List[ColumnOrdering]]) -> Self
Set the ordering for the results.

Parameters:

ordering (Optional[List[ColumnOrdering]]) – The ordering to use for the results. If None, then the default ordering will be used.
fast_search
¶

fast_search() -> Self
Skip searching un-indexed data.

This can make queries faster, but will miss any data that has not been indexed.

Tip

You can add new data into an existing index by calling AsyncTable.optimize.

postfilter
¶

postfilter() -> Self
If this is called then filtering will happen after the search instead of before. By default filtering will be performed before the search. This is how filtering is typically understood to work. This prefilter step does add some additional latency. Creating a scalar index on the filter column(s) can often improve this latency. However, sometimes a filter is too complex or scalar indices cannot be applied to the column. In these cases postfiltering can be used instead of prefiltering to improve latency. Post filtering applies the filter to the results of the search. This means we only run the filter on a much smaller set of data. However, it can cause the query to return fewer than limit results (or even no results) if none of the nearest results match the filter. Post filtering happens during the "refine stage" (described in more detail in @see {@link VectorQuery#refineFactor}). This means that setting a higher refine factor can often help restore some of the results lost by post filtering.

rerank
¶

rerank(reranker: Reranker = RRFReranker(), query_string: Optional[str] = None) -> AsyncHybridQuery
nearest_to_text
¶

nearest_to_text(query: str | FullTextQuery, columns: Union[str, List[str], None] = None) -> AsyncHybridQuery
Find the documents that are most relevant to the given text query, in addition to vector search.

This converts the vector query into a hybrid query.

This search will perform a full text search on the table and return the most relevant documents, combined with the vector query results. The text relevance is determined by BM25.

The columns to search must be with native FTS index (Tantivy-based can't work with this method).

By default, all indexed columns are searched, now only one column can be searched at a time.

Parameters:

query (str | FullTextQuery) – The text query to search for.
columns (Union[str, List[str], None], default: None ) – The columns to search in. If None, all indexed columns are searched. For now only one column can be searched at a time.
to_batches
¶

to_batches(*, max_batch_length: Optional[int] = None, timeout: Optional[timedelta] = None) -> AsyncRecordBatchReader
lancedb.query.AsyncFTSQuery
¶
Bases: AsyncStandardQuery

A query for full text search for LanceDB.

to_query_object
¶

to_query_object() -> Query
Convert the query into a query object

This is currently experimental but can be useful as the query object is pure python and more easily serializable.

select
¶

select(columns: Union[List[str], dict[str, str]]) -> Self
Return only the specified columns.

By default a query will return all columns from the table. However, this can have a very significant impact on latency. LanceDb stores data in a columnar fashion. This means we can finely tune our I/O to select exactly the columns we need.

As a best practice you should always limit queries to the columns that you need. If you pass in a list of column names then only those columns will be returned.

You can also use this method to create new "dynamic" columns based on your existing columns. For example, you may not care about "a" or "b" but instead simply want "a + b". This is often seen in the SELECT clause of an SQL query (e.g. SELECT a+b FROM my_table).

To create dynamic columns you can pass in a dict[str, str]. A column will be returned for each entry in the map. The key provides the name of the column. The value is an SQL string used to specify how the column is calculated.

For example, an SQL query might state SELECT a + b AS combined, c. The equivalent input to this method would be {"combined": "a + b", "c": "c"}.

Columns will always be returned in the order given, even if that order is different than the order used when adding the data.

with_row_id
¶

with_row_id() -> Self
Include the _rowid column in the results.

with_row_address
¶

with_row_address(with_row_address: bool = True) -> Self
Include the _rowaddr column in scanner-backed plain query results.

with_fragments
¶

with_fragments(fragments: Any) -> Self
Restrict scanner-backed plain query results to the given Lance fragments.

fragment_ids
¶

fragment_ids(fragment_ids: List[int]) -> Self
Restrict scanner-backed plain query results to the given Lance fragment ids.

output_schema
¶

output_schema() -> Schema
Return the output schema for the query

This does not execute the query.

to_arrow
¶

to_arrow(timeout: Optional[timedelta] = None) -> Table
Execute the query and collect the results into an Apache Arrow Table.

This method will collect all results into memory before returning. If you expect a large number of results, you may want to use to_batches

Parameters:

timeout (Optional[timedelta], default: None ) – The maximum time to wait for the query to complete. If not specified, no timeout is applied. If the query does not complete within the specified time, an error will be raised.
to_list
¶

to_list(timeout: Optional[timedelta] = None) -> List[dict]
Execute the query and return the results as a list of dictionaries.

Each list entry is a dictionary with the selected column names as keys, or all table columns if select is not called. The vector and the "_distance" fields are returned whether or not they're explicitly selected.

Parameters:

timeout (Optional[timedelta], default: None ) – The maximum time to wait for the query to complete. If not specified, no timeout is applied. If the query does not complete within the specified time, an error will be raised.
to_pandas
¶

to_pandas(flatten: Optional[Union[int, bool]] = None, timeout: Optional[timedelta] = None, *, blob_mode: BlobMode = 'lazy', **kwargs) -> 'pd.DataFrame'
Execute the query and collect the results into a pandas DataFrame.

This method will collect all results into memory before returning. If you expect a large number of results, you may want to use to_batches and convert each batch to pandas separately.

Examples:


>>> import asyncio
>>> from lancedb import connect_async
>>> async def doctest_example():
...     conn = await connect_async("./.lancedb")
...     table = await conn.create_table("my_table", data=[{"a": 1, "b": 2}])
...     async for batch in await table.query().to_batches():
...         batch_df = batch.to_pandas()
>>> asyncio.run(doctest_example())
Parameters:

flatten (Optional[Union[int, bool]], default: None ) – If flatten is True, flatten all nested columns. If flatten is an integer, flatten the nested columns up to the specified depth. If unspecified, do not flatten the nested columns.
timeout (Optional[timedelta], default: None ) – The maximum time to wait for the query to complete. If not specified, no timeout is applied. If the query does not complete within the specified time, an error will be raised.
blob_mode (BlobMode, default: 'lazy' ) – Controls how blob columns are returned for plain scan queries. Vector, FTS, hybrid, and other non-native query shapes keep the existing Arrow conversion path and only support blob descriptions.
**kwargs – Forwarded to pyarrow.Table.to_pandas after query execution and optional flattening.
to_polars
¶

to_polars(timeout: Optional[timedelta] = None) -> 'pl.DataFrame'
Execute the query and collect the results into a Polars DataFrame.

This method will collect all results into memory before returning. If you expect a large number of results, you may want to use to_batches and convert each batch to polars separately.

Parameters:

timeout (Optional[timedelta], default: None ) – The maximum time to wait for the query to complete. If not specified, no timeout is applied. If the query does not complete within the specified time, an error will be raised.
Examples:


>>> import asyncio
>>> import polars as pl
>>> from lancedb import connect_async
>>> async def doctest_example():
...     conn = await connect_async("./.lancedb")
...     table = await conn.create_table("my_table", data=[{"a": 1, "b": 2}])
...     async for batch in await table.query().to_batches():
...         batch_df = pl.from_arrow(batch)
>>> asyncio.run(doctest_example())
to_pydantic
¶

to_pydantic(model: Type[LanceModel], *, timeout: Optional[timedelta] = None) -> List[LanceModel]
Convert results to a list of pydantic models.

Parameters:

model (Type[LanceModel]) – The pydantic model to use.
timeout (timedelta, default: None ) – The maximum time to wait for the query to complete. If None, wait indefinitely.
Returns:

list[LanceModel] –
explain_plan
¶

explain_plan(verbose: Optional[bool] = False)
Return the execution plan for this query.

Examples:


>>> import asyncio
>>> from lancedb import connect_async
>>> async def doctest_example():
...     conn = await connect_async("./.lancedb")
...     table = await conn.create_table("my_table", [{"vector": [99.0, 99.0]}])
...     plan = await table.query().nearest_to([1.0, 2.0]).explain_plan(True)
...     print(plan)
>>> asyncio.run(doctest_example())
ProjectionExec: expr=[vector@0 as vector, _distance@2 as _distance]
  GlobalLimitExec: skip=0, fetch=10
    FilterExec: _distance@2 IS NOT NULL
      SortExec: TopK(fetch=10), expr=[_distance@2 ASC NULLS LAST, _rowid@1 ASC NULLS LAST], preserve_partitioning=[false]
        KNNVectorDistance: metric=l2
          LanceRead: uri=..., projection=[vector], ...
Parameters:

verbose (bool, default: False ) – Use a verbose output format.
Returns:

plan ( str ) –
analyze_plan
¶

analyze_plan()
Execute the query and display with runtime metrics.

Returns:

plan ( str ) –
where
¶

where(predicate: Union[str, Expr]) -> Self
Only return rows matching the given predicate

The predicate can be a SQL string or a type-safe :class:~lancedb.expr.Expr built with :func:~lancedb.expr.col and :func:~lancedb.expr.lit.

Examples:


>>> predicate = "x > 10"
>>> predicate = "y > 0 AND y < 100"
>>> predicate = "x > 5 OR y = 'test'"
Filtering performance can often be improved by creating a scalar index on the filter column(s).

Calling this multiple times combines the filters with a logical AND rather than replacing the previous filter.

limit
¶

limit(limit: int) -> Self
Set the maximum number of results to return.

By default, a plain search has no limit. If this method is not called then every valid row from the table will be returned.

offset
¶

offset(offset: int) -> Self
Set the offset for the results.

Parameters:

offset (int) – The offset to start fetching results from.
order_by
¶

order_by(ordering: Optional[List[ColumnOrdering]]) -> Self
Set the ordering for the results.

Parameters:

ordering (Optional[List[ColumnOrdering]]) – The ordering to use for the results. If None, then the default ordering will be used.
fast_search
¶

fast_search() -> Self
Skip searching un-indexed data.

This can make queries faster, but will miss any data that has not been indexed.

Tip

You can add new data into an existing index by calling AsyncTable.optimize.

postfilter
¶

postfilter() -> Self
If this is called then filtering will happen after the search instead of before. By default filtering will be performed before the search. This is how filtering is typically understood to work. This prefilter step does add some additional latency. Creating a scalar index on the filter column(s) can often improve this latency. However, sometimes a filter is too complex or scalar indices cannot be applied to the column. In these cases postfiltering can be used instead of prefiltering to improve latency. Post filtering applies the filter to the results of the search. This means we only run the filter on a much smaller set of data. However, it can cause the query to return fewer than limit results (or even no results) if none of the nearest results match the filter. Post filtering happens during the "refine stage" (described in more detail in @see {@link VectorQuery#refineFactor}). This means that setting a higher refine factor can often help restore some of the results lost by post filtering.

get_query
¶

get_query() -> str
rerank
¶

rerank(reranker: Reranker = RRFReranker()) -> AsyncFTSQuery
nearest_to
¶

nearest_to(query_vector: Union[VEC, Tuple, List[VEC]]) -> AsyncHybridQuery
In addition doing text search on the LanceDB Table, also find the nearest vectors to the given query vector.

This converts the query from a FTS Query to a Hybrid query. Results from the vector search will be combined with results from the FTS query.

This method will attempt to convert the input to the query vector expected by the embedding model. If the input cannot be converted then an error will be thrown.

By default, there is no embedding model, and the input should be something that can be converted to a pyarrow array of floats. This includes lists, numpy arrays, and tuples.

If there is only one vector column (a column whose data type is a fixed size list of floats) then the column does not need to be specified. If there is more than one vector column you must use AsyncVectorQuery.column to specify which column you would like to compare with.

If no index has been created on the vector column then a vector query will perform a distance comparison between the query vector and every vector in the database and then sort the results. This is sometimes called a "flat search"

For small databases, with tens of thousands of vectors or less, this can be reasonably fast. In larger databases you should create a vector index on the column. If there is a vector index then an "approximate" nearest neighbor search (frequently called an ANN search) will be performed. This search is much faster, but the results will be approximate.

The query can be further parameterized using the returned builder. There are various ANN search parameters that will let you fine tune your recall accuracy vs search latency.

Hybrid searches always have a limit. If limit has not been called then a default limit of 10 will be used.

Typically, a single vector is passed in as the query. However, you can also pass in multiple vectors. This can be useful if you want to find the nearest vectors to multiple query vectors. This is not expected to be faster than making multiple queries concurrently; it is just a convenience method. If multiple vectors are passed in then an additional column query_index will be added to the results. This column will contain the index of the query vector that the result is nearest to.

to_batches
¶

to_batches(*, max_batch_length: Optional[int] = None, timeout: Optional[timedelta] = None) -> AsyncRecordBatchReader
lancedb.query.AsyncHybridQuery
¶
Bases: AsyncStandardQuery, AsyncVectorQueryBase

A query builder that performs hybrid vector and full text search. Results are combined and reranked based on the specified reranker. By default, the results are reranked using the RRFReranker, which uses reciprocal rank fusion score for reranking.

To make the vector and fts results comparable, the scores are normalized. Instead of normalizing scores, the normalize parameter can be set to "rank" in the rerank method to convert the scores to ranks and then normalize them.

column
¶

column(column: str) -> Self
Set the vector column to query

This controls which column is compared to the query vector supplied in the call to AsyncQuery.nearest_to.

This parameter must be specified if the table has more than one column whose data type is a fixed-size-list of floats.

nprobes
¶

nprobes(nprobes: int) -> Self
Set the number of partitions to search (probe)

This argument is only used when the vector column has an IVF-based index. If there is no index then this value is ignored.

The IVF stage of IVF PQ divides the input into partitions (clusters) of related values.

The partition whose centroids are closest to the query vector will be exhaustiely searched to find matches. This parameter controls how many partitions should be searched.

Increasing this value will increase the recall of your query but will also increase the latency of your query. The default value is 20. This default is good for many cases but the best value to use will depend on your data and the recall that you need to achieve.

For best results we recommend tuning this parameter with a benchmark against your actual data to find the smallest possible value that will still give you the desired recall.

minimum_nprobes
¶

minimum_nprobes(minimum_nprobes: int) -> Self
Set the minimum number of probes to use.

See nprobes for more details.

These partitions will be searched on every indexed vector query and will increase recall at the expense of latency.

maximum_nprobes
¶

maximum_nprobes(maximum_nprobes: int) -> Self
Set the maximum number of probes to use.

See nprobes for more details.

If this value is greater than minimum_nprobes then the excess partitions will be searched only if we have not found enough results.

This can be useful when there is a narrow filter to allow these queries to spend more time searching and avoid potential false negatives.

If this value is 0 then no limit will be applied and all partitions could be searched if needed to satisfy the limit.

distance_range
¶

distance_range(lower_bound: Optional[float] = None, upper_bound: Optional[float] = None) -> Self
Set the distance range to use.

Only rows with distances within range [lower_bound, upper_bound) will be returned.

Parameters:

lower_bound (Optional[float], default: None ) – The lower bound of the distance range.
upper_bound (Optional[float], default: None ) – The upper bound of the distance range.
Returns:

AsyncVectorQuery – The AsyncVectorQuery object.
ef
¶

ef(ef: int) -> Self
Set the number of candidates to consider during search

This argument is only used when the vector column has an HNSW index. If there is no index then this value is ignored.

Increasing this value will increase the recall of your query but will also increase the latency of your query. The default value is 1.5 * limit. This default is good for many cases but the best value to use will depend on your data and the recall that you need to achieve.

refine_factor
¶

refine_factor(refine_factor: int) -> Self
A multiplier to control how many additional rows are taken during the refine step

This argument is only used when the vector column has an IVF PQ index. If there is no index then this value is ignored.

An IVF PQ index stores compressed (quantized) values. They query vector is compared against these values and, since they are compressed, the comparison is inaccurate.

This parameter can be used to refine the results. It can improve both improve recall and correct the ordering of the nearest results.

To refine results LanceDb will first perform an ANN search to find the nearest limit * refine_factor results. In other words, if refine_factor is 3 and limit is the default (10) then the first 30 results will be selected. LanceDb then fetches the full, uncompressed, values for these 30 results. The results are then reordered by the true distance and only the nearest 10 are kept.

Note: there is a difference between calling this method with a value of 1 and never calling this method at all. Calling this method with any value will have an impact on your search latency. When you call this method with a refine_factor of 1 then LanceDb still needs to fetch the full, uncompressed, values so that it can potentially reorder the results.

Note: if this method is NOT called then the distances returned in the _distance column will be approximate distances based on the comparison of the quantized query vector and the quantized result vectors. This can be considerably different than the true distance between the query vector and the actual uncompressed vector.

distance_type
¶

distance_type(distance_type: str) -> Self
Set the distance metric to use

When performing a vector search we try and find the "nearest" vectors according to some kind of distance metric. This parameter controls which distance metric to use. See @see {@link IvfPqOptions.distanceType} for more details on the different distance metrics available.

Note: if there is a vector index then the distance type used MUST match the distance type used to train the vector index. If this is not done then the results will be invalid.

By default "l2" is used.

bypass_vector_index
¶

bypass_vector_index() -> Self
If this is called then any vector index is skipped

An exhaustive (flat) search will be performed. The query vector will be compared to every vector in the table. At high scales this can be expensive. However, this is often still useful. For example, skipping the vector index can give you ground truth results which you can use to calculate your recall to select an appropriate value for nprobes.

to_query_object
¶

to_query_object() -> Query
Convert the query into a query object

This is currently experimental but can be useful as the query object is pure python and more easily serializable.

select
¶

select(columns: Union[List[str], dict[str, str]]) -> Self
Return only the specified columns.

By default a query will return all columns from the table. However, this can have a very significant impact on latency. LanceDb stores data in a columnar fashion. This means we can finely tune our I/O to select exactly the columns we need.

As a best practice you should always limit queries to the columns that you need. If you pass in a list of column names then only those columns will be returned.

You can also use this method to create new "dynamic" columns based on your existing columns. For example, you may not care about "a" or "b" but instead simply want "a + b". This is often seen in the SELECT clause of an SQL query (e.g. SELECT a+b FROM my_table).

To create dynamic columns you can pass in a dict[str, str]. A column will be returned for each entry in the map. The key provides the name of the column. The value is an SQL string used to specify how the column is calculated.

For example, an SQL query might state SELECT a + b AS combined, c. The equivalent input to this method would be {"combined": "a + b", "c": "c"}.

Columns will always be returned in the order given, even if that order is different than the order used when adding the data.

with_row_id
¶

with_row_id() -> Self
Include the _rowid column in the results.

with_row_address
¶

with_row_address(with_row_address: bool = True) -> Self
Include the _rowaddr column in scanner-backed plain query results.

with_fragments
¶

with_fragments(fragments: Any) -> Self
Restrict scanner-backed plain query results to the given Lance fragments.

fragment_ids
¶

fragment_ids(fragment_ids: List[int]) -> Self
Restrict scanner-backed plain query results to the given Lance fragment ids.

output_schema
¶

output_schema() -> Schema
Return the output schema for the query

This does not execute the query.

to_arrow
¶

to_arrow(timeout: Optional[timedelta] = None) -> Table
Execute the query and collect the results into an Apache Arrow Table.

This method will collect all results into memory before returning. If you expect a large number of results, you may want to use to_batches

Parameters:

timeout (Optional[timedelta], default: None ) – The maximum time to wait for the query to complete. If not specified, no timeout is applied. If the query does not complete within the specified time, an error will be raised.
to_list
¶

to_list(timeout: Optional[timedelta] = None) -> List[dict]
Execute the query and return the results as a list of dictionaries.

Each list entry is a dictionary with the selected column names as keys, or all table columns if select is not called. The vector and the "_distance" fields are returned whether or not they're explicitly selected.

Parameters:

timeout (Optional[timedelta], default: None ) – The maximum time to wait for the query to complete. If not specified, no timeout is applied. If the query does not complete within the specified time, an error will be raised.
to_pandas
¶

to_pandas(flatten: Optional[Union[int, bool]] = None, timeout: Optional[timedelta] = None, *, blob_mode: BlobMode = 'lazy', **kwargs) -> 'pd.DataFrame'
Execute the query and collect the results into a pandas DataFrame.

This method will collect all results into memory before returning. If you expect a large number of results, you may want to use to_batches and convert each batch to pandas separately.

Examples:


>>> import asyncio
>>> from lancedb import connect_async
>>> async def doctest_example():
...     conn = await connect_async("./.lancedb")
...     table = await conn.create_table("my_table", data=[{"a": 1, "b": 2}])
...     async for batch in await table.query().to_batches():
...         batch_df = batch.to_pandas()
>>> asyncio.run(doctest_example())
Parameters:

flatten (Optional[Union[int, bool]], default: None ) – If flatten is True, flatten all nested columns. If flatten is an integer, flatten the nested columns up to the specified depth. If unspecified, do not flatten the nested columns.
timeout (Optional[timedelta], default: None ) – The maximum time to wait for the query to complete. If not specified, no timeout is applied. If the query does not complete within the specified time, an error will be raised.
blob_mode (BlobMode, default: 'lazy' ) – Controls how blob columns are returned for plain scan queries. Vector, FTS, hybrid, and other non-native query shapes keep the existing Arrow conversion path and only support blob descriptions.
**kwargs – Forwarded to pyarrow.Table.to_pandas after query execution and optional flattening.
to_polars
¶

to_polars(timeout: Optional[timedelta] = None) -> 'pl.DataFrame'
Execute the query and collect the results into a Polars DataFrame.

This method will collect all results into memory before returning. If you expect a large number of results, you may want to use to_batches and convert each batch to polars separately.

Parameters:

timeout (Optional[timedelta], default: None ) – The maximum time to wait for the query to complete. If not specified, no timeout is applied. If the query does not complete within the specified time, an error will be raised.
Examples:


>>> import asyncio
>>> import polars as pl
>>> from lancedb import connect_async
>>> async def doctest_example():
...     conn = await connect_async("./.lancedb")
...     table = await conn.create_table("my_table", data=[{"a": 1, "b": 2}])
...     async for batch in await table.query().to_batches():
...         batch_df = pl.from_arrow(batch)
>>> asyncio.run(doctest_example())
to_pydantic
¶

to_pydantic(model: Type[LanceModel], *, timeout: Optional[timedelta] = None) -> List[LanceModel]
Convert results to a list of pydantic models.

Parameters:

model (Type[LanceModel]) – The pydantic model to use.
timeout (timedelta, default: None ) – The maximum time to wait for the query to complete. If None, wait indefinitely.
Returns:

list[LanceModel] –
where
¶

where(predicate: Union[str, Expr]) -> Self
Only return rows matching the given predicate

The predicate can be a SQL string or a type-safe :class:~lancedb.expr.Expr built with :func:~lancedb.expr.col and :func:~lancedb.expr.lit.

Examples:


>>> predicate = "x > 10"
>>> predicate = "y > 0 AND y < 100"
>>> predicate = "x > 5 OR y = 'test'"
Filtering performance can often be improved by creating a scalar index on the filter column(s).

Calling this multiple times combines the filters with a logical AND rather than replacing the previous filter.

limit
¶

limit(limit: int) -> Self
Set the maximum number of results to return.

By default, a plain search has no limit. If this method is not called then every valid row from the table will be returned.

offset
¶

offset(offset: int) -> Self
Set the offset for the results.

Parameters:

offset (int) – The offset to start fetching results from.
order_by
¶

order_by(ordering: Optional[List[ColumnOrdering]]) -> Self
Set the ordering for the results.

Parameters:

ordering (Optional[List[ColumnOrdering]]) – The ordering to use for the results. If None, then the default ordering will be used.
fast_search
¶

fast_search() -> Self
Skip searching un-indexed data.

This can make queries faster, but will miss any data that has not been indexed.

Tip

You can add new data into an existing index by calling AsyncTable.optimize.

postfilter
¶

postfilter() -> Self
If this is called then filtering will happen after the search instead of before. By default filtering will be performed before the search. This is how filtering is typically understood to work. This prefilter step does add some additional latency. Creating a scalar index on the filter column(s) can often improve this latency. However, sometimes a filter is too complex or scalar indices cannot be applied to the column. In these cases postfiltering can be used instead of prefiltering to improve latency. Post filtering applies the filter to the results of the search. This means we only run the filter on a much smaller set of data. However, it can cause the query to return fewer than limit results (or even no results) if none of the nearest results match the filter. Post filtering happens during the "refine stage" (described in more detail in @see {@link VectorQuery#refineFactor}). This means that setting a higher refine factor can often help restore some of the results lost by post filtering.

rerank
¶

rerank(reranker: Reranker = RRFReranker(), normalize: str = 'score') -> AsyncHybridQuery
Rerank the hybrid search results using the specified reranker. The reranker must be an instance of Reranker class.

Parameters:

reranker (Reranker, default: RRFReranker() ) – The reranker to use. Must be an instance of Reranker class.
normalize (str, default: 'score' ) – The method to normalize the scores. Can be "rank" or "score". If "rank", the scores are converted to ranks and then normalized. If "score", the scores are normalized directly.
Returns:

AsyncHybridQuery – The AsyncHybridQuery object.
to_batches
¶

to_batches(*, max_batch_length: Optional[int] = None, timeout: Optional[timedelta] = None) -> AsyncRecordBatchReader
explain_plan
¶

explain_plan(verbose: Optional[bool] = False)
Return the execution plan for this query.

The output includes both the vector and FTS search plans.

Examples:


>>> import asyncio
>>> from lancedb import connect_async
>>> from lancedb.index import FTS
>>> async def doctest_example():
...     conn = await connect_async("./.lancedb")
...     table = await conn.create_table("my_table", [{"vector": [99.0, 99.0], "text": "hello world"}])
...     await table.create_index("text", config=FTS(with_position=False))
...     plan = await table.query().nearest_to([1.0, 2.0]).nearest_to_text("hello").explain_plan(True)
...     print(plan)
>>> asyncio.run(doctest_example())
RRFReranker(K=60)
    ProjectionExec: expr=[vector@0 as vector, text@3 as text, _distance@2 as _distance]
      Take: columns="vector, _rowid, _distance, (text)"
        CoalesceBatchesExec: target_batch_size=1024
          GlobalLimitExec: skip=0, fetch=10
            FilterExec: _distance@2 IS NOT NULL
              SortExec: TopK(fetch=10), expr=[_distance@2 ASC NULLS LAST, _rowid@1 ASC NULLS LAST], preserve_partitioning=[false]
                KNNVectorDistance: metric=l2
                  LanceRead: uri=..., projection=[vector], ...
    ProjectionExec: expr=[vector@2 as vector, text@3 as text, _score@1 as _score]
      Take: columns="_rowid, _score, (vector), (text)"
        CoalesceBatchesExec: target_batch_size=1024
          GlobalLimitExec: skip=0, fetch=10
            MatchQuery: column=text, query=hello
Parameters:

verbose (bool, default: False ) – Use a verbose output format.
Returns:

plan ( str ) –
analyze_plan
¶

analyze_plan()
Execute the query and return the physical execution plan with runtime metrics.

This runs both the vector and FTS (full-text search) queries and returns detailed metrics for each step of execution—such as rows processed, elapsed time, I/O stats, and more. It’s useful for debugging and performance analysis.

Returns:

plan ( str ) –