# Querying lance-ns tables from DuckDB

The lakehouse is **Lance-on-S3**, so any engine that can read the Lance format can consume the governed
output directly — no export, no bespoke reader. DuckDB does this through its **core `lance` extension**
([lance-format/lance-duckdb](https://github.com/lance-format/lance-duckdb)). This is a **read-side analytics
consumer**, not an embedded query engine for the platform (the in-process query/consumption engine stays
parked, `#20`); it is the "an analyst can point DuckDB at our tables" path.

Proven live against **pylance 8.0.0** datasets over the deployed RustFS store — regression-guarded by
`tests/e2e/test_duckdb_lance_e2e.py` (runs in the `e2e-stack` CI job).

## Install

```sql
INSTALL lance;   -- DuckDB core extension repository
LOAD lance;
```

## Credentials for the object store (RustFS / S3 / MinIO)

The extension registers a `TYPE LANCE` secret and forwards its keys to lance-core's object store. For the
in-cluster RustFS (or any S3-compatible store on a custom endpoint) the two non-obvious settings are
**`ENDPOINT` must carry the scheme** (`http://…` — lance-core's `object_store` panics on a scheme-less URI)
and **`ALLOW_HTTP true`** for a plain-HTTP endpoint:

```sql
CREATE SECRET rfs (
    TYPE LANCE,
    ACCESS_KEY_ID 'rustfsadmin',
    SECRET_ACCESS_KEY 'rustfsadmin',
    REGION 'us-east-1',
    ENDPOINT 'http://localhost:9900',   -- port-forward: kubectl port-forward svc/lance-ns-rustfs 9900:9000
    ALLOW_HTTP true
);
```

`SCOPE 's3://bucket/prefix'` narrows a secret to a URI prefix (the extension matches by longest prefix), so
you can register different credentials per warehouse/bucket. For real AWS, drop `ENDPOINT`/`ALLOW_HTTP` and
use `PROVIDER credential_chain` to inherit the SDK chain.

## Read a dataset

lance-ns catalog datasets are stored as `s3://<warehouse-bucket>/<hash>_<namespace>$<table>` — they carry
**no `.lance` suffix**, so DuckDB's suffix-based replacement scan (`FROM 'path/to/x.lance'`) does not fire.
Use the explicit table function **`__lance_scan(uri)`**:

```sql
SELECT count(*)              FROM __lance_scan('s3://lance-catalog/1a9b5ce0_gov1378264$silver');
SELECT kind, sum(score)      FROM __lance_scan('s3://mb-a/…') GROUP BY kind;
DESCRIBE SELECT *            FROM __lance_scan('s3://…');   -- the Lance schema, in column order
```

A dataset written *with* a `.lance` suffix (e.g. `COPY (…) TO 'out.lance' (FORMAT lance)`) can be read with
the bare `FROM 'out.lance'` replacement scan.

The extension also exposes Lance's search primitives — `lance_fts(...)`, `lance_vector_search(...)`,
`lance_hybrid_search(...)` — so full-text / vector / hybrid retrieval over a Lance table is available from SQL
without leaving DuckDB.

## Version / compatibility

- The `lance` extension is versioned per **DuckDB release** (built by DuckDB's CI); it has historically lagged
  new DuckDB versions. If `INSTALL lance` 404s for a very new DuckDB, use the DuckDB version the extension is
  published for, or build from source (`duckdb -unsigned -c "LOAD 'build/release/extension/lance/lance.duckdb_extension'"`).
- Format compat with the **pylance version the platform writes** (currently 8.0.0) is the thing that can break
  on a lance-core bump — `test_duckdb_lance_e2e.py` writes with pylance and reads back with the extension,
  asserting count + values + schema, so a format regression fails CI rather than surfacing downstream.
